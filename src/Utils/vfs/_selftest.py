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
    effective_shadow_data_root,
    effective_shadow_root,
    effective_tool_data_root,
    effective_tool_game_root,
    finalize_deployment,
    has_deployment_state,
    prefer_virtual_executable,
    virtual_root_write_path,
    wrap_command,
)
from Utils.deploy import (  # noqa: E402
    CustomRule,
    LinkMode,
    RestoreIncompleteError,
    cleanup_custom_deploy_dirs,
    deploy_custom_rules,
    deploy_filemap,
    deploy_root_folder,
    restore_custom_rules,
    restore_root_folder,
)
from Utils.quick_configure import (  # noqa: E402
    build_quick_configure_options,
    deploy_mode_change_blocked,
)
from Utils.launch_handoff import build_launch_handoff  # noqa: E402
from Utils.exe_launch import (  # noqa: E402
    is_game_launch_exe,
    launch_game,
    run_tool_logged,
)
from Utils.xedit_tools import (  # noqa: E402
    begin_xedit_vfs_session,
    persist_xedit_vfs_changes,
)
from Games.Bethesda.fallout_3 import Fallout_3  # noqa: E402
from Games.BepInEx.BepInEx import (  # noqa: E402
    Subnautica,
    Subnautica_Below_Zero,
    Valheim,
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

_cyberpunk_spec = importlib.util.spec_from_file_location(
    "amethyst_vfs_selftest_cyberpunk_2077",
    _SRC_ROOT / "Games" / "Cyberpunk 2077" / "cyberpunk_2077.py",
)
if _cyberpunk_spec is None or _cyberpunk_spec.loader is None:
    raise RuntimeError("Could not load the Cyberpunk 2077 game handler.")
_cyberpunk_module = importlib.util.module_from_spec(_cyberpunk_spec)
_cyberpunk_spec.loader.exec_module(_cyberpunk_module)
Cyberpunk2077 = _cyberpunk_module.Cyberpunk2077

_witcher_spec = importlib.util.spec_from_file_location(
    "amethyst_vfs_selftest_witcher_3",
    _SRC_ROOT / "Games" / "The Witcher 3" / "witcher_3.py",
)
if _witcher_spec is None or _witcher_spec.loader is None:
    raise RuntimeError("Could not load The Witcher 3 game handler.")
_witcher_module = importlib.util.module_from_spec(_witcher_spec)
_witcher_spec.loader.exec_module(_witcher_module)
Witcher3 = _witcher_module.Witcher3


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

    def get_effective_mod_staging_path(self):
        return self.staging

    def get_effective_filemap_path(self):
        return self.profiles / "filemap.txt"

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


class _FakeNativeBepInExGame(_FakeSubnauticaGame):
    """A native Unity build using the shared BepInEx VFS launch hooks."""

    exe_name = "NativeBepInEx.exe"
    exe_name_alts = ["NativeBepInEx.x86_64"]
    # Steam-client startup has its own mocked lifecycle test. Keep command-
    # shaping fixtures hermetic even though the real BepInEx base opts in.
    native_steam_client_required = False


class _FakeNativeDirectGame(_FakeGame):
    """A plain native player used to exercise Play's direct/None route."""

    exe_name = "NativeDirectGame"
    exe_name_alts: list[str] = []
    direct_launch_exes: list[str] = []
    default_launch_args = ["--default-argument"]


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


class _FakeCyberpunkGame(_FakeCustomPaths, Cyberpunk2077):
    """Temporary install retaining Cyberpunk's real root-deploy contract."""

    def __init__(self, root: Path):
        _FakeCustomPaths.__init__(self, root, {})


class _FakeWitcher3Game(_FakeCustomPaths, Witcher3):
    """Temporary install retaining Witcher's routed filemap contract."""

    def __init__(self, root: Path):
        _FakeCustomPaths.__init__(self, root, {})


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


def test_shadow_capture_survives_configured_path_change() -> None:
    """Capture the old fixed view after the configured install has changed."""
    with tempfile.TemporaryDirectory() as tmp:
        game = _FakeGame(Path(tmp))
        (game.game / "root.txt").write_text("old-root", encoding="utf-8")
        (game.game / "Data" / "data.txt").write_text(
            "old-data", encoding="utf-8")
        filemap = game.profiles / "filemap.txt"
        filemap.write_text("", encoding="utf-8")

        def _build() -> Path:
            build_layers(
                game,
                profile="default",
                filemap=filemap,
                staging=game.staging,
                per_mod_strip={},
                per_mod_deploy={},
                raw_mods=None,
                excluded_raw=None,
                root_folder_enabled=False,
            )
            finalize_deployment(game)
            return game.profile / STATE_DIR_NAME / "view"

        old_view = _build()
        (old_view / "runtime-old-root.txt").write_text(
            "old root runtime", encoding="utf-8")
        (old_view / "Data" / "runtime-old-data.txt").write_text(
            "old data runtime", encoding="utf-8")

        # Configure a different install before redeploy. Capture must use the
        # old manifest's data-relative path inside the fixed profile view; it
        # must never walk either recorded/current physical install.
        new_game = Path(tmp) / "new-game"
        (new_game / "Data").mkdir(parents=True)
        (new_game / "root.txt").write_text("new-root", encoding="utf-8")
        (new_game / "Data" / "data.txt").write_text(
            "new-data", encoding="utf-8")
        game.game = new_game
        new_view = _build()

        root_upper = game.profile / STATE_DIR_NAME / "root-upper"
        assert (root_upper / "runtime-old-root.txt").read_text(
            encoding="utf-8") == "old root runtime"
        assert (game.overwrite / "runtime-old-data.txt").read_text(
            encoding="utf-8") == "old data runtime"
        assert (new_view / "runtime-old-root.txt").is_file()
        assert (new_view / "Data" / "runtime-old-data.txt").is_file()

        # Exercise Restore's capture path under a second configured-root
        # change as well; cleanup must preserve both newly-created files.
        (new_view / "runtime-new-root.txt").write_text(
            "new root runtime", encoding="utf-8")
        (new_view / "Data" / "runtime-new-data.txt").write_text(
            "new data runtime", encoding="utf-8")
        third_game = Path(tmp) / "third-game"
        (third_game / "Data").mkdir(parents=True)
        game.game = third_game
        cleanup_deployment(game, preserve_upper=True)

        assert (root_upper / "runtime-new-root.txt").read_text(
            encoding="utf-8") == "new root runtime"
        assert (game.overwrite / "runtime-new-data.txt").read_text(
            encoding="utf-8") == "new data runtime"
        assert not (game.profile / STATE_DIR_NAME / MANIFEST_NAME).exists()
        assert (Path(tmp) / "game" / "root.txt").read_text(
            encoding="utf-8") == "old-root"
        assert (new_game / "root.txt").read_text(
            encoding="utf-8") == "new-root"
    print("✓ shadow runtime capture survives configured path changes")


def test_failed_post_view_hook_never_promotes_partial_output() -> None:
    """An unfinalized generation is deploy output, never runtime output.

    ``build_layers`` publishes the replacement view before handler-specific
    post-view hooks run. If one of those hooks fails, Restore or a following
    deploy must discard that generation without comparing it to an older
    snapshot and promoting its new files into Overwrite/root-upper.
    """
    definition = {
        "name": "Post-view Failure VFS Test",
        "game_id": "post_view_failure_vfs_test",
        "exe_name": "Game.exe",
        "deploy_type": "standard",
        "mod_data_path": "Mods",
    }

    def _exercise(*, redeploy: bool) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            game = _FakeStandardCustomGame(Path(tmp), definition)

            def _pipeline_deploy() -> None:
                # The real deploy pipeline defers ordinary snapshot requests.
                # Transactional shadow snapshots must bypass that queue so the
                # incomplete marker is never cleared ahead of the disk write.
                game.begin_deferred_runtime_snapshot()
                try:
                    _deploy_custom_fixture(game, profile="default")
                finally:
                    _generic, requests = game.end_deferred_runtime_snapshot()
                    assert not requests

            data = game.get_mod_data_path()
            assert data is not None
            data.mkdir(parents=True)
            (game.game / "Game.exe").write_text(
                "vanilla executable", encoding="utf-8")
            (data / "vanilla.txt").write_text("vanilla", encoding="utf-8")

            old = game.staging / "Old" / "old.txt"
            old.parent.mkdir(parents=True)
            old.write_text("old generation", encoding="utf-8")
            (game.profile / "modlist.txt").write_text(
                "+Old\n", encoding="utf-8")
            game.filemap.write_text("old.txt\tOld\n", encoding="utf-8")
            _pipeline_deploy()

            state = game.profile / STATE_DIR_NAME
            assert not (state / "view.incomplete").exists()

            new = game.staging / "New" / "new.txt"
            new.parent.mkdir(parents=True)
            new.write_text("new generation", encoding="utf-8")
            root_payload = game.root_folder / "new-root-payload.txt"
            root_payload.write_text("new root generation", encoding="utf-8")
            (game.profile / "modlist.txt").write_text(
                "+New\n", encoding="utf-8")
            game.filemap.write_text("new.txt\tNew\n", encoding="utf-8")

            def _fail_after_partial_output(*, view_root: Path,
                                           **_kwargs) -> None:
                (view_root / "Mods" / "hook-partial-data.txt").write_text(
                    "partial data output", encoding="utf-8")
                (view_root / "hook-partial-root.txt").write_text(
                    "partial root output", encoding="utf-8")
                raise RuntimeError("injected post-view hook failure")

            game._vfs_post_view_build = _fail_after_partial_output
            try:
                _pipeline_deploy()
            except RuntimeError as exc:
                assert "injected post-view hook failure" in str(exc)
            else:
                raise AssertionError("the failing post-view hook was ignored")

            failed_view = effective_shadow_root(game)
            assert (state / "view.incomplete").is_file()
            assert (failed_view / "Mods" / "new.txt").is_file()
            assert (failed_view / "new-root-payload.txt").is_file()
            assert (failed_view / "Mods" / "hook-partial-data.txt").is_file()
            assert (failed_view / "hook-partial-root.txt").is_file()

            if redeploy:
                final = game.staging / "Final" / "final.txt"
                final.parent.mkdir(parents=True)
                final.write_text("final generation", encoding="utf-8")
                root_payload.unlink()
                (game.profile / "modlist.txt").write_text(
                    "+Final\n", encoding="utf-8")
                game.filemap.write_text(
                    "final.txt\tFinal\n", encoding="utf-8")
                game._vfs_post_view_build = None
                _pipeline_deploy()

                final_view = effective_shadow_root(game)
                assert (final_view / "Mods" / "final.txt").is_file()
                assert not (final_view / "Mods" / "new.txt").exists()
                assert not (final_view / "new-root-payload.txt").exists()
                assert not (final_view / "Mods"
                            / "hook-partial-data.txt").exists()
                assert not (final_view / "hook-partial-root.txt").exists()
                assert not (state / "view.incomplete").exists()
            else:
                logs: list[str] = []
                cleanup_deployment(
                    game, preserve_upper=True, log_fn=logs.append)
                assert not has_deployment_state(game)
                assert any("unfinalized view" in line for line in logs)

            # These paths came from the failed deployment or its hook. Neither
            # recovery route may turn them into persistent user/runtime state.
            for relative in ("new.txt", "hook-partial-data.txt"):
                assert not (game.overwrite / relative).exists()
            root_upper = state / "root-upper"
            for relative in ("new-root-payload.txt", "hook-partial-root.txt"):
                assert not (root_upper / relative).exists()

            if redeploy:
                cleanup_deployment(game, preserve_upper=True)

    _exercise(redeploy=False)
    _exercise(redeploy=True)

    # Snapshot publication is itself part of the transaction. A failed disk
    # write must leave the marker in place even while the deploy pipeline is
    # deferring ordinary snapshots, so cleanup discards the view safely.
    with tempfile.TemporaryDirectory() as tmp:
        game = _FakeStandardCustomGame(Path(tmp), definition)
        data = game.get_mod_data_path()
        assert data is not None
        data.mkdir(parents=True)
        (game.game / "Game.exe").write_text("vanilla", encoding="utf-8")
        staged = game.staging / "SnapshotFail" / "snapshot-fail.txt"
        staged.parent.mkdir(parents=True)
        staged.write_text("deploy output", encoding="utf-8")
        (game.profile / "modlist.txt").write_text(
            "+SnapshotFail\n", encoding="utf-8")
        game.filemap.write_text(
            "snapshot-fail.txt\tSnapshotFail\n", encoding="utf-8")

        game.begin_deferred_runtime_snapshot()
        try:
            with patch(
                "Utils.vfs.overlay._write_deploy_snapshot",
                side_effect=OSError("injected snapshot write failure"),
            ) as snapshot_writer:
                try:
                    _deploy_custom_fixture(game, profile="default")
                except OSError as exc:
                    assert "injected snapshot write failure" in str(exc)
                else:
                    raise AssertionError("snapshot write failure was ignored")
                assert snapshot_writer.call_args.kwargs["strict"] is True
        finally:
            _generic, requests = game.end_deferred_runtime_snapshot()
            assert not requests

        state = game.profile / STATE_DIR_NAME
        assert (state / "view.incomplete").is_file()
        cleanup_deployment(game, preserve_upper=True)
        assert not (game.overwrite / "snapshot-fail.txt").exists()
        assert not (state / "root-upper" / "snapshot-fail.txt").exists()

    print("✓ failed post-view output is discarded on Restore and redeploy")


def test_vfs_root_payload_preserves_physical_recovery() -> None:
    """A private Root_Folder build must not consume physical restore state."""
    with tempfile.TemporaryDirectory() as tmp:
        game = _FakeGame(Path(tmp))
        vanilla = game.game / "root.dll"
        vanilla.write_text("vanilla", encoding="utf-8")
        payload = game.root_folder / "root.dll"
        payload.write_text("physical mod", encoding="utf-8")

        deploy_root_folder(
            game.root_folder, game.game, mode=LinkMode.HARDLINK)
        physical_log = game.profiles / "root_folder_deployed.txt"
        physical_identities = game.profiles / "root_deploy_identities.json"
        physical_backup = game.profiles / "Root_Backup" / "root.dll"
        assert vanilla.read_text(encoding="utf-8") == "physical mod"
        assert physical_log.is_file()
        assert physical_identities.is_file()
        assert physical_backup.read_text(encoding="utf-8") == "vanilla"

        filemap = game.profiles / "filemap.txt"
        filemap.write_text("", encoding="utf-8")
        build_layers(
            game,
            profile="default",
            filemap=filemap,
            staging=game.staging,
            per_mod_strip={},
            per_mod_deploy={},
            raw_mods=None,
            excluded_raw=None,
            root_folder_enabled=True,
        )
        state = game.profile / STATE_DIR_NAME
        assert (state / "view" / "root.dll").read_text() == "physical mod"

        # The VFS build used disposable bookkeeping; the older physical
        # journal and original remain available for the migration restore.
        assert physical_log.is_file()
        assert physical_identities.is_file()
        assert physical_backup.read_text(encoding="utf-8") == "vanilla"
        cleanup_deployment(game, preserve_upper=True)
        assert restore_root_folder(game.root_folder, game.game) == 1
        assert vanilla.read_text(encoding="utf-8") == "vanilla"
        assert not physical_log.exists()
        assert not physical_identities.exists()
        assert not (game.profiles / "Root_Backup").exists()
    print("✓ VFS Root_Folder metadata preserves physical recovery state")


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


def test_wizard_tools_use_vfs_and_xedit_edits_persist() -> None:
    """External tools see the bound view; xEdit saves never touch vanilla."""
    with tempfile.TemporaryDirectory() as tmp:
        game = _FakeGame(Path(tmp))
        vanilla = game.game / "Data" / "Skyrim.esm"
        vanilla.write_text("vanilla-master", encoding="utf-8")
        staged = game.staging / "Plugin Mod" / "Test.esp"
        staged.parent.mkdir(parents=True)
        staged.write_text("original-plugin", encoding="utf-8")
        body_exe = (game.staging / "BodySlide"
                    / "CalienteTools/BodySlide/BodySlide.exe")
        body_exe.parent.mkdir(parents=True)
        body_exe.write_text("tool", encoding="utf-8")
        (body_exe.parent / "res/xrc").mkdir(parents=True)
        filemap = game.get_effective_filemap_path()
        filemap.write_text(
            "Test.esp\tPlugin Mod\n"
            "CalienteTools/BodySlide/BodySlide.exe\tBodySlide\n",
            encoding="utf-8",
        )

        build_layers(
            game, profile="default", filemap=filemap, staging=game.staging,
            per_mod_strip={}, per_mod_deploy={}, raw_mods=None,
            excluded_raw=None, root_folder_enabled=False,
        )
        view_data = effective_shadow_data_root(game)
        assert effective_tool_game_root(game) == effective_shadow_root(game)
        assert effective_tool_data_root(game) == view_data
        from Utils.bodyslide_tools import find_deployed_exe
        assert find_deployed_exe(game, "BodySlide.exe") == (
            view_data / "CalienteTools/BodySlide/BodySlide.exe")
        session = begin_xedit_vfs_session(game)
        assert session is not None and session.data_dir == view_data
        assert not os.path.samefile(view_data / "Test.esp", staged)
        assert not os.path.samefile(view_data / "Skyrim.esm", vanilla)

        (view_data / "Test.esp").write_text(
            "cleaned-plugin", encoding="utf-8")
        (view_data / "Skyrim.esm").write_text(
            "cleaned-vanilla", encoding="utf-8")
        (view_data / "CreatedByXEdit.esl").write_text(
            "new-plugin", encoding="utf-8")
        assert persist_xedit_vfs_changes(game, session) == 3
        assert staged.read_text(encoding="utf-8") == "cleaned-plugin"
        assert vanilla.read_text(encoding="utf-8") == "vanilla-master"
        assert (game.overwrite / "Skyrim.esm").read_text() == "cleaned-vanilla"
        assert (game.overwrite / "CreatedByXEdit.esl").read_text() == "new-plugin"
        assert "Test.esp" in (
            staged.parent / "meta.ini").read_text(encoding="utf-8")
        assert persist_xedit_vfs_changes(game, session) == 0

        fake_proc = types.SimpleNamespace(stdout=[], wait=lambda: 0)
        with patch(
            "Utils.steam_finder.proton_run_command",
            return_value=["proton", "runinprefix", "/tools/SSEEdit.exe"],
        ), patch(
            "Utils.vfs.wrap_command", return_value=["vfs-wrapped-tool"],
        ) as wrap, patch(
            "Utils.exe_launch.subprocess.Popen", return_value=fake_proc,
        ) as popen:
            rc = run_tool_logged(
                Path("/proton"), Path("/tools/SSEEdit.exe"), {}, game=game)
        assert rc == 0
        wrap.assert_called_once()
        assert wrap.call_args.args[0] is game
        assert any(
            call.args and call.args[0] == ["vfs-wrapped-tool"]
            for call in popen.call_args_list
        )

        cleanup_deployment(game, preserve_upper=True)
        assert vanilla.read_text(encoding="utf-8") == "vanilla-master"
    print("✓ wizard tools use the VFS view and xEdit edits persist")


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


def test_symlinked_vfs_state_root_is_never_followed() -> None:
    """Build and cleanup must not traverse a redirected state directory."""
    with tempfile.TemporaryDirectory() as tmp:
        game = _FakeGame(Path(tmp))
        external = Path(tmp) / "external-state"
        for relative, content in (
            ("manifest.json", "outside manifest"),
            ("pending", "outside pending"),
            ("lower/keep.txt", "outside lower"),
            ("lower.build/keep.txt", "outside build"),
            ("view/keep.txt", "outside view"),
            ("root-upper/keep.txt", "outside upper"),
        ):
            target = external / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")

        state = game.profile / STATE_DIR_NAME
        state.symlink_to(external, target_is_directory=True)
        before = {
            path.relative_to(external).as_posix(): path.read_bytes()
            for path in external.rglob("*") if path.is_file()
        }

        for operation in (
            lambda: cleanup_deployment(game),
            lambda: build_layers(
                game,
                profile="default",
                filemap=game.profiles / "filemap.txt",
                staging=game.staging,
                per_mod_strip={},
                per_mod_deploy={},
                raw_mods=None,
                excluded_raw=None,
                root_folder_enabled=False,
            ),
            lambda: wrap_command(game, ["/bin/true"]),
        ):
            (game.profiles / "filemap.txt").write_text("", encoding="utf-8")
            try:
                operation()
            except RuntimeError as exc:
                assert "symlinked profile VFS state directory" in str(exc)
            else:
                raise AssertionError("symlinked VFS state root was accepted")

            after = {
                path.relative_to(external).as_posix(): path.read_bytes()
                for path in external.rglob("*") if path.is_file()
            }
            assert after == before
            assert state.is_symlink()

        # Keep the unsafe state visible to the deployment guard so Restore
        # reports the refusal instead of silently treating it as undeployed.
        assert has_deployment_state(game)
        assert "default" in deployment_state_profiles(game)
    print("✓ symlinked VFS state roots are refused without external deletion")


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
        (game.overwrite / "upper-collision.txt").write_text(
            "overwrite-loses-to-root")
        (game.overwrite / "routed-overwrite.dll").write_text(
            "routed-overwrite")

        root_winner = game.root_folder / "Content" / "Mods" / "normal.txt"
        root_winner.parent.mkdir(parents=True)
        root_winner.write_text("root-folder-wins")
        (game.root_folder / "Content" / "Mods"
         / "upper-collision.txt").write_text("root-folder-final-winner")
        (game.root_folder / "root-config.ini").write_text("root-config")
        (game.profile / "modlist.txt").write_text(
            "+Normal\n+RootRule\n+DataRule\n", encoding="utf-8")
        game.filemap.write_text(
            "normal.txt\tNormal\n"
            "root-loader.dll\tRootRule\n"
            "nested/asset.route\tDataRule\n"
            "overwrite-only.txt\t[Overwrite]\n"
            "upper-collision.txt\t[Overwrite]\n"
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
        assert (view_data / "upper-collision.txt").read_text() == \
            "root-folder-final-winner"
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
        assert not game.vfs_direct_shadow_launch

        # Enabling the remaining BepInEx subclasses must not alter the
        # established Windows launch contract: no Unix loader is injected and
        # the game-root bind remains the wrapper around the vanilla command.
        windows_passthrough = game.get_vfs_passthrough_command([
            "/usr/bin/env", str(game.game / "Subnautica.exe"),
        ])
        assert not any(
            Path(token).name.casefold() in {
                "run_bepinex.sh", "start_game_bepinex.sh",
            }
            for token in windows_passthrough
        )
        assert "bwrap" in [Path(token).name for token in windows_passthrough]

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
        assert below_zero.supports_profile_vfs
    print("✓ Windows BepInEx routing, subclass coverage, and launch behavior")


def test_native_bepinex_shadow_launch() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        game = _FakeNativeBepInExGame(Path(tmp))
        # Unified Steam depots such as Inscryption ship both players. The
        # primary .exe therefore exists even though Steam's Linux default and
        # the deployed Unix BepInEx pack must select the native alternative.
        windows_exe = game.game / game.exe_name
        windows_exe.write_text("windows player", encoding="utf-8")
        native_exe = game.game / game.exe_name_alts[0]
        native_exe.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        native_exe.chmod(0o755)

        plugin = game.staging / "NativeMod" / "NativeMod.dll"
        plugin.parent.mkdir(parents=True)
        plugin.write_text("plugin")
        pack = game.staging / "BepInExPack"
        pack.mkdir()
        loader_body = (
            "#!/bin/sh\n"
            "test -f BepInEx/plugins/NativeMod.dll || exit 41\n"
            "exec \"$@\"\n"
        )
        # Include both supported spellings. The generic base prefers the stock
        # run_bepinex script; Valheim reverses this order for its pack.
        for launcher in ("run_bepinex.sh", "start_game_bepinex.sh"):
            (pack / launcher).write_text(loader_body, encoding="utf-8")

        (game.profile / "modlist.txt").write_text(
            "+NativeMod\n+BepInExPack\n", encoding="utf-8")
        filemap = game.profiles / "filemap.txt"
        filemap.write_text(
            "NativeMod.dll\tNativeMod\n"
            "run_bepinex.sh\tBepInExPack\n"
            "start_game_bepinex.sh\tBepInExPack\n",
            encoding="utf-8",
        )

        mod_files_stub = types.ModuleType("Utils.mod_files")
        mod_files_stub.excluded_raw_by_mod = lambda _profile: {}
        with patch.dict(sys.modules, {"Utils.mod_files": mod_files_stub}):
            game.deploy(profile="default")

        state = game.profile / STATE_DIR_NAME
        view = state / "view"
        view_native = view / native_exe.name
        view_run = view / "run_bepinex.sh"
        view_start = view / "start_game_bepinex.sh"
        assert view_native.is_file()
        assert view_run.is_file() and view_start.is_file()
        assert not (game.game / "run_bepinex.sh").exists()
        assert game.supports_profile_vfs
        assert game.vfs_direct_shadow_launch
        assert game.get_vfs_launch_exe() == native_exe
        assert game._vfs_native_launcher() == game.game / "run_bepinex.sh"
        assert Valheim.vfs_native_launcher_names.fget(
            Valheim.__new__(Valheim))[0] == "start_game_bepinex.sh"

        # Manager Play selects the native game binary, then the handler puts
        # the virtual loader in front of that same vanilla command. The direct
        # shadow wrapper retargets both paths and sets cwd to the private view.
        inherited_env = {
            "SteamAppId": "999999",
            "SteamGameId": "999999",
            "SteamOverlayGameId": "999999",
            "STEAM_COMPAT_APP_ID": "999999",
        }
        with patch("Utils.exe_launch.spawn_process_watched") as spawn, \
                patch("Utils.exe_launch.launch_exe_via_proton") as proton, \
                patch("Utils.exe_launch.game_is_steam_install",
                      return_value=True), \
                patch("Utils.exe_launch.effective_steam_id",
                      return_value="1092790"), \
                patch("Utils.exe_launch.load_exe_args",
                      side_effect=lambda _game, key: (
                          "--saved-argument"
                          if key == game.exe_name else "--wrong-native-key"
                      )) as load_args, \
                patch("Utils.exe_launch.load_launch_options",
                      side_effect=lambda _game, key: (
                          "BEP_TEST_ENV=profile /usr/bin/env %command% "
                          "--launch-suffix"
                          if key == game.exe_name else "--wrong-option-key"
                      )) as launch_options, \
                patch("Utils.exe_launch.steam_launch_options_for_game") \
                      as steam_options, \
                patch.object(game, "default_launch_args_for_exe",
                             return_value=["--default-argument"]), \
                patch("Utils.xdg.host_env", return_value=dict(inherited_env)):
            launch_game(game)
        proton.assert_not_called()
        steam_options.assert_not_called()
        load_args.assert_called_once_with(game, game.exe_name)
        launch_options.assert_called_once_with(game, game.exe_name)
        spawn.assert_called_once()
        manager_command = spawn.call_args.args[0]
        manager_env = spawn.call_args.kwargs["env"]
        assert all(manager_env[key] == "1092790" for key in inherited_env)
        assert manager_env["BEP_TEST_ENV"] == "profile"
        assert manager_command.count(str(view_run)) == 1
        assert str(view_native) in manager_command
        assert str(native_exe) not in manager_command
        loader_index = manager_command.index(str(view_run))
        native_index = manager_command.index(str(view_native))
        env_index = manager_command.index("/usr/bin/env")
        assert manager_command[loader_index - 1] == "/bin/sh"
        assert env_index < loader_index < native_index
        assert manager_command[native_index + 1:] == [
            "--default-argument",
            "--saved-argument",
            "--launch-suffix",
        ]
        result = subprocess.run(
            manager_command, text=True, capture_output=True, check=False)
        assert result.returncode == 0, result.stderr

        # With no per-executable value, native VFS Play inherits Steam's
        # launch options just like the normal store/direct routes.
        with patch("Utils.exe_launch.spawn_process_watched") as spawn, \
                patch("Utils.exe_launch.game_is_steam_install",
                      return_value=True), \
                patch("Utils.exe_launch.effective_steam_id",
                      return_value="1092790"), \
                patch("Utils.exe_launch.load_exe_args",
                      side_effect=lambda _game, key: (
                          "" if key == game.exe_name else "--wrong-native-key"
                      )) as load_args, \
                patch("Utils.exe_launch.load_launch_options",
                      side_effect=lambda _game, key: (
                          "" if key == game.exe_name else "--wrong-option-key"
                      )) as launch_options, \
                patch("Utils.exe_launch.steam_launch_options_for_game",
                      return_value=(
                          "STEAM_FALLBACK_ENV=1 /usr/bin/env %command% "
                          "--steam-fallback"
                      )) as steam_options, \
                patch.object(game, "default_launch_args_for_exe",
                             return_value=[]), \
                patch("Utils.xdg.host_env", return_value=dict(inherited_env)):
            launch_game(game)
        steam_options.assert_called_once()
        load_args.assert_called_once_with(game, game.exe_name)
        launch_options.assert_called_once_with(game, game.exe_name)
        fallback_command = spawn.call_args.args[0]
        fallback_env = spawn.call_args.kwargs["env"]
        assert fallback_env["STEAM_FALLBACK_ENV"] == "1"
        fallback_loader = fallback_command.index(str(view_run))
        fallback_native = fallback_command.index(str(view_native))
        fallback_env_wrapper = fallback_command.index("/usr/bin/env")
        assert fallback_env_wrapper < fallback_loader < fallback_native
        assert fallback_command[fallback_native + 1:] == ["--steam-fallback"]

        # A launcher handoff keeps wrapper tokens ahead of the BepInEx script,
        # then inserts the script immediately before the native executable.
        # Unix BepInEx treats its first argument as the executable to inject
        # into, so prefixing the whole wrapper command would target the wrapper
        # rather than the Unity player.
        vanilla = [
            "steam-native-wrapper", "--", str(native_exe), "--test-argument",
        ]
        passthrough = game.get_vfs_passthrough_command(vanilla)
        assert "bwrap" not in [Path(token).name for token in passthrough]
        assert passthrough.count(str(view_run)) == 1
        assert str(view_native) in passthrough
        assert "steam-native-wrapper" in passthrough
        assert "--test-argument" in passthrough
        native_index = passthrough.index(str(view_native))
        assert passthrough[native_index - 2:native_index] == [
            "/bin/sh", str(view_run),
        ]
        assert passthrough.index("steam-native-wrapper") < native_index - 2

        executable_prefix = game.get_vfs_passthrough_command([
            "/usr/bin/env", str(native_exe),
        ])
        result = subprocess.run(
            executable_prefix, text=True, capture_output=True, check=False)
        assert result.returncode == 0, result.stderr

        # A user retaining an old BepInEx launch option must not get a second
        # copy of the loader when Amethyst's launcher wrapper is added.
        already_wrapped = game.get_vfs_passthrough_command([
            str(game.game / "run_bepinex.sh"), str(native_exe),
        ])
        assert sum(
            Path(token).name.casefold() == "run_bepinex.sh"
            for token in already_wrapped
        ) == 1

        # The generic game-folder tool path also calls wrap_launch_command;
        # it must not accidentally be converted into a native game launch.
        tool_command = game.wrap_launch_command(["/tmp/BepInExTool.exe"])
        assert not any(
            Path(token).name.casefold() in {
                "run_bepinex.sh", "start_game_bepinex.sh",
            }
            for token in tool_command
        )

        with patch.object(game, "_vfs_native_game_exe",
                          return_value=native_exe), \
                patch.object(game, "_vfs_native_launcher", return_value=None):
            try:
                game.get_vfs_passthrough_command([str(native_exe)])
            except RuntimeError as exc:
                assert "native BepInEx launch script is missing" in str(exc)
            else:
                raise AssertionError("missing native BepInEx script was accepted")

        game.restore()
        assert not (state / MANIFEST_NAME).exists()

    # Valheim's legacy physical deploy tells users to put the script directly
    # in Steam's Launch Options. That command cannot see a profile-only view;
    # VFS mode must point users at Amethyst's launcher-aware handoff instead.
    valheim = Valheim.__new__(Valheim)
    messages: list[str] = []
    with patch.object(Subnautica, "deploy", return_value=None), \
            patch.object(Valheim, "vfs_launch_enabled", True), \
            patch.object(Valheim, "_vfs_native_game_exe",
                         return_value=Path("/game/valheim.x86_64")):
        valheim.deploy(log_fn=messages.append)
    assert any("automatically wraps Valheim" in message for message in messages)
    assert not any("Steam launch option" in message for message in messages)

    messages.clear()
    with patch.object(Subnautica, "deploy", return_value=None), \
            patch.object(Valheim, "vfs_launch_enabled", True), \
            patch.object(Valheim, "_vfs_native_game_exe", return_value=None):
        valheim.deploy(log_fn=messages.append)
    assert not any("automatically wraps Valheim" in message for message in messages)
    assert any("private game view" in message for message in messages)
    print("✓ native BepInEx manager and launcher-passthrough wrapping")


def test_native_none_launch_steam_context() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        game = _FakeNativeDirectGame(Path(tmp))
        game.native_steam_client_required = True
        native_exe = game.game / game.exe_name
        native_exe.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        native_exe.chmod(0o755)
        inherited_env = {
            "SteamAppId": "999999",
            "SteamGameId": "999999",
            "SteamOverlayGameId": "999999",
            "STEAM_COMPAT_APP_ID": "999999",
        }
        launch_order: list[str] = []

        with patch("Utils.exe_launch.spawn_process_watched") as spawn, \
                patch("Utils.exe_launch.load_launch_mode",
                      return_value="none"), \
                patch("Utils.exe_launch.game_is_steam_install",
                      return_value=True), \
                patch("Utils.exe_launch.effective_steam_id",
                      return_value="2868840"), \
                patch("Utils.exe_launch.load_exe_args",
                      return_value="--saved-argument") as load_args, \
                patch("Utils.exe_launch.load_launch_options",
                      return_value=(
                          "DIRECT_TEST_ENV=profile /usr/bin/env %command% "
                          "--launch-suffix"
                      )) as launch_options, \
                patch("Utils.exe_launch.steam_launch_options_for_game") \
                      as steam_options, \
                patch("Utils.steam_client.ensure_steam_client_running",
                      side_effect=lambda **_kwargs: (
                          launch_order.append("client-ready") or True
                      )) as ensure_steam, \
                patch("Utils.xdg.host_env", return_value=dict(inherited_env)):
            spawn.side_effect = lambda *_args, **_kwargs: (
                launch_order.append("game-spawn"))
            launch_game(game)

        spawn.assert_called_once()
        ensure_steam.assert_called_once()
        assert launch_order == ["client-ready", "game-spawn"]
        load_args.assert_called_once_with(game, native_exe.name)
        launch_options.assert_called_once_with(game, native_exe.name)
        steam_options.assert_not_called()
        command = spawn.call_args.args[0]
        env = spawn.call_args.kwargs["env"]
        assert all(env[key] == "2868840" for key in inherited_env)
        assert env["DIRECT_TEST_ENV"] == "profile"
        assert spawn.call_args.kwargs["cwd"] == native_exe.parent

        env_index = command.index("/usr/bin/env")
        exe_index = command.index(str(native_exe))
        assert env_index < exe_index
        assert command[exe_index + 1:] == [
            "--default-argument",
            "--saved-argument",
            "--launch-suffix",
        ]

        # Amethyst itself may have been started as a Steam shortcut. A native
        # game outside Steam must not inherit that unrelated app identity.
        inherited_nonsteam = {
            "PATH": "/test/bin",
            "SteamAppId": "999999",
            "SteamGameId": "999999",
            "SteamOverlayGameId": "999999",
            "STEAM_COMPAT_APP_ID": "999999",
        }
        with patch("Utils.exe_launch.spawn_process_watched") as spawn, \
                patch("Utils.exe_launch.load_launch_mode",
                      return_value="none"), \
                patch("Utils.exe_launch.game_is_steam_install",
                      return_value=False), \
                patch("Utils.exe_launch.effective_steam_id",
                      return_value=""), \
                patch("Utils.exe_launch.load_exe_args",
                      return_value=""), \
                patch("Utils.exe_launch.load_launch_options",
                      return_value=""), \
                patch("Utils.exe_launch.steam_launch_options_for_game",
                      return_value=""), \
                patch("Utils.steam_client.ensure_steam_client_running") \
                      as ensure_steam, \
                patch("Utils.xdg.host_env",
                      return_value=dict(inherited_nonsteam)):
            launch_game(game)
        spawn.assert_called_once()
        ensure_steam.assert_not_called()
        nonsteam_command = spawn.call_args.args[0]
        nonsteam_env = spawn.call_args.kwargs["env"]
        assert str(native_exe) in nonsteam_command
        assert all(key not in nonsteam_env for key in (
            "SteamAppId", "SteamGameId", "SteamOverlayGameId",
            "STEAM_COMPAT_APP_ID",
        ))
        assert nonsteam_env["PATH"] == "/test/bin"
        assert not (native_exe.parent / "steam_appid.txt").exists()
    print("✓ native None launch pins or clears inherited Steam context")


def test_native_steam_client_lifecycle() -> None:
    from Utils.steam_client import ensure_steam_client_running

    # An existing client is accepted without touching any launcher process.
    already_messages: list[str] = []
    with patch("Utils.steam_client.steam_client_running",
               return_value=True) as running, \
            patch("Utils.steam_client.subprocess.Popen") as client_spawn:
        assert ensure_steam_client_running(log_fn=already_messages.append)
    running.assert_called_once_with(strict=True)
    client_spawn.assert_not_called()
    assert any("already running" in message for message in already_messages)

    # A Flatpak-host PID that cannot be queried is intentionally ambiguous:
    # config writers retain the historical conservative True, while native
    # Steamworks launch readiness must fail its strict proof.
    from Utils.steam_finder import steam_client_running
    with tempfile.TemporaryDirectory() as tmp:
        fake_home = Path(tmp)
        pid_file = fake_home / ".steam" / "steam.pid"
        pid_file.parent.mkdir(parents=True)
        pid_file.write_text("424242\n", encoding="ascii")
        real_exists = Path.exists

        def _flatpak_exists(path: Path) -> bool:
            if str(path) == "/.flatpak-info":
                return True
            return real_exists(path)

        with patch("Utils.steam_finder._HOME", fake_home), \
                patch.object(Path, "exists", autospec=True,
                             side_effect=_flatpak_exists), \
                patch("shutil.which", return_value=None):
            assert steam_client_running()
            assert not steam_client_running(strict=True)

    # A closed client is started without a game URI. Process liveness is
    # checked again after the PID appears, covering Steam's bootstrap/PID
    # replacement window without claiming that login or IPC is ready.
    with tempfile.TemporaryDirectory() as tmp:
        fake_home = Path(tmp)
        inherited = {
            "PATH": "/test/bin",
            "DISPLAY": ":99",
            "SteamAppId": "999999",
            "SteamGameId": "999999",
            "SteamOverlayGameId": "999999",
            "STEAM_COMPAT_APP_ID": "999999",
            "SteamAppUser": "wrong-user",
            "SteamUser": "wrong-user",
            "SteamClientLaunch": "1",
            "SteamEnv": "1",
            "STEAM_COMPAT_DATA_PATH": "/wrong/prefix",
            "STEAM_RUNTIME": "/wrong/runtime",
            "STEAM_RUNTIME_LIBRARY_PATH": "/wrong/runtime/lib",
            "PRESSURE_VESSEL_FILESYSTEMS_RW": "/wrong/mount",
        }
        ready_messages: list[str] = []
        client_proc = types.SimpleNamespace(poll=lambda: None)
        with patch("Utils.steam_client.Path") as path_type, \
                patch("Utils.steam_client.steam_client_running",
                      side_effect=[False, True, True]) as running, \
                patch("Utils.steam_client.shutil.which",
                      side_effect=lambda name: f"/usr/bin/{name}"), \
                patch("Utils.steam_client.subprocess.Popen", side_effect=[
                    OSError("injected stale xdg-open association"),
                    OSError("injected missing native Steam"),
                    client_proc,
                ]) as client_spawn, \
                patch("Utils.steam_client.time.sleep") as sleep, \
                patch("Utils.xdg.host_env",
                      return_value=dict(inherited)):
            path_type.home.return_value = fake_home
            path_type.return_value.exists.return_value = False
            assert ensure_steam_client_running(
                log_fn=ready_messages.append, timeout=5.0)
        running.assert_called()
        assert running.call_count == 3
        assert all(call.kwargs == {"strict": True}
                   for call in running.call_args_list)
        sleep.assert_called_once_with(2.0)
        assert [call.args[0] for call in client_spawn.call_args_list] == [
            ["xdg-open", "steam://open/main"],
            ["steam", "-silent"],
            ["flatpak", "run", "com.valvesoftware.Steam", "-silent"],
        ]
        client_env = client_spawn.call_args_list[-1].kwargs["env"]
        assert client_env["PATH"] == "/test/bin"
        assert client_env["DISPLAY"] == ":99"
        assert all(key not in client_env for key in (
            "SteamAppId", "SteamGameId", "SteamOverlayGameId",
            "STEAM_COMPAT_APP_ID", "SteamAppUser", "SteamUser",
            "SteamClientLaunch", "SteamEnv", "STEAM_COMPAT_DATA_PATH",
            "STEAM_RUNTIME", "STEAM_RUNTIME_LIBRARY_PATH",
            "PRESSURE_VESSEL_FILESYSTEMS_RW",
        ))
        assert client_spawn.call_args_list[-1].kwargs["cwd"] == str(fake_home)
        assert any("client process is running" in message
                   for message in ready_messages)

    # Launcher failure is returned to Manager Play, which must fail closed and
    # report an actionable error instead of starting a Steamworks game anyway.
    failed_messages: list[str] = []
    with tempfile.TemporaryDirectory() as tmp:
        fake_home = Path(tmp)
        with patch("Utils.steam_client.Path") as path_type, \
                patch("Utils.steam_client.steam_client_running",
                      return_value=False), \
                patch("Utils.steam_client.shutil.which",
                      side_effect=lambda name: (
                          "/usr/bin/xdg-open" if name == "xdg-open" else None
                      )), \
                patch("Utils.steam_client.subprocess.Popen",
                      side_effect=OSError("injected client start failure")), \
                patch("Utils.xdg.host_env", return_value={}):
            path_type.home.return_value = fake_home
            path_type.return_value.exists.return_value = False
            assert not ensure_steam_client_running(
                log_fn=failed_messages.append, timeout=1.0)
        assert any("could not start Steam" in message
                   for message in failed_messages)

        game = _FakeNativeDirectGame(Path(tmp) / "launch")
        game.native_steam_client_required = True
        native_exe = game.game / game.exe_name
        native_exe.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        native_exe.chmod(0o755)
        launch_messages: list[str] = []
        with patch("Utils.exe_launch.spawn_process_watched") as game_spawn, \
                patch("Utils.exe_launch.load_launch_mode",
                      return_value="none"), \
                patch("Utils.exe_launch.game_is_steam_install",
                      return_value=True), \
                patch("Utils.exe_launch.effective_steam_id",
                      return_value="2868840"), \
                patch("Utils.exe_launch.load_exe_args", return_value=""), \
                patch("Utils.exe_launch.load_launch_options",
                      return_value=""), \
                patch("Utils.exe_launch.steam_launch_options_for_game",
                      return_value=""), \
                patch("Utils.steam_client.ensure_steam_client_running",
                      return_value=False) as ensure_steam, \
                patch("Utils.exe_launch.launch_report.actionable",
                      side_effect=lambda reason: reason), \
                patch("Utils.exe_launch.launch_report.mark_failed") \
                      as mark_failed, \
                patch("Utils.xdg.host_env", return_value={}):
            launch_game(game, log_fn=launch_messages.append)
        ensure_steam.assert_called_once()
        game_spawn.assert_not_called()
        mark_failed.assert_called_once()
        assert "Steam is required" in mark_failed.call_args.args[0]
        assert any("refusing to launch" in message
                   for message in launch_messages)
        assert not (native_exe.parent / "steam_appid.txt").exists()

        # Opt-in is handler-specific. A Steam install without it must retain
        # the ordinary native launch path and never call the client helper.
        game.native_steam_client_required = False
        with patch("Utils.exe_launch.spawn_process_watched") as game_spawn, \
                patch("Utils.exe_launch.load_launch_mode",
                      return_value="none"), \
                patch("Utils.exe_launch.game_is_steam_install",
                      return_value=True), \
                patch("Utils.exe_launch.effective_steam_id",
                      return_value="2868840"), \
                patch("Utils.exe_launch.load_exe_args", return_value=""), \
                patch("Utils.exe_launch.load_launch_options",
                      return_value=""), \
                patch("Utils.exe_launch.steam_launch_options_for_game",
                      return_value=""), \
                patch("Utils.steam_client.ensure_steam_client_running") \
                      as ensure_steam, \
                patch("Utils.xdg.host_env", return_value={}):
            launch_game(game)
        ensure_steam.assert_not_called()
        game_spawn.assert_called_once()
    print("✓ native Steam client startup, readiness, and failure lifecycle")


def test_native_vfs_flatpak_forwards_launch_environment() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        game = _FakeNativeDirectGame(Path(tmp))
        native_exe = game.game / game.exe_name
        native_exe.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        native_exe.chmod(0o755)

        state = _write_manifest(game)
        manifest_path = state / MANIFEST_NAME
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        view = state / "view"
        (view / "Data").mkdir(parents=True)
        (view / native_exe.name).write_text(
            "#!/bin/sh\nexit 0\n", encoding="utf-8")
        manifest.update({
            "backend": BACKEND_SHADOW,
            "view_root": str(view),
        })
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

        game.vfs_launch_enabled = True
        game.vfs_direct_shadow_launch = True
        game.native_steam_client_required = True
        game.get_vfs_launch_exe = lambda: native_exe
        game.wrap_launch_command = (
            lambda command, *, env=None: wrap_command(
                game, command, env=env)
        )
        inherited_env = {
            "SteamAppId": "999999",
            "SteamGameId": "999999",
            "SteamOverlayGameId": "999999",
            "STEAM_COMPAT_APP_ID": "999999",
        }

        # Give the launch-option variable a different sandbox baseline so the
        # real forwarding filter must recognise it as an explicit override.
        with patch.dict(os.environ, {
                "FLATPAK_LAUNCH_OPTION": "sandbox-baseline",
             }), patch("Utils.exe_launch.spawn_process_watched") as spawn, \
                patch("Utils.exe_launch.game_is_steam_install",
                      return_value=True), \
                patch("Utils.exe_launch.effective_steam_id",
                      return_value="2868840"), \
                patch("Utils.exe_launch.load_exe_args", return_value=""), \
                patch("Utils.exe_launch.load_launch_options", return_value=(
                    "FLATPAK_LAUNCH_OPTION=profile /usr/bin/env %command%"
                )), \
                patch("Utils.steam_client.ensure_steam_client_running",
                      return_value=True) as ensure_steam, \
                patch("Utils.xdg.host_env",
                      return_value=dict(inherited_env)), \
                patch("Utils.vfs.overlay._inside_flatpak",
                      return_value=True):
            launch_game(game)

        spawn.assert_called_once()
        ensure_steam.assert_called_once()
        command = spawn.call_args.args[0]
        assert command[:2] == ["flatpak-spawn", "--host"]
        host_index = command.index("--host")
        shell_index = command.index("/bin/sh")
        forwarded = [
            "--env=SteamAppId=2868840",
            "--env=SteamGameId=2868840",
            "--env=SteamOverlayGameId=2868840",
            "--env=STEAM_COMPAT_APP_ID=2868840",
            "--env=FLATPAK_LAUNCH_OPTION=profile",
        ]
        for token in forwarded:
            assert host_index < command.index(token) < shell_index
        assert str(view / native_exe.name) in command
        assert command.count("flatpak-spawn") == 1
        assert not (game.game / "steam_appid.txt").exists()
        assert not (view / "steam_appid.txt").exists()
    print("✓ native Flatpak VFS forwards Steam and launch-option environment")


def test_native_steam_handoff_fallback_is_not_recursive() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        game = _FakeNativeDirectGame(Path(tmp))
        game.game_id = "native_direct_test"
        native_exe = game.game / game.exe_name
        native_exe.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        native_exe.chmod(0o755)
        inherited_env = {
            "SteamAppId": "999999",
            "SteamGameId": "999999",
            "SteamOverlayGameId": "999999",
            "STEAM_COMPAT_APP_ID": "999999",
        }
        generated_handoff = (
            "/usr/bin/flatpak-spawn --host /usr/bin/python3 "
            "'/home/test/Amethyst Mod Manager/src/cli.py' "
            "launch native_direct_test --profile default -- %command%"
        )
        messages: list[str] = []

        with patch("Utils.exe_launch.spawn_process_watched") as spawn, \
                patch("Utils.exe_launch.load_launch_mode",
                      return_value="none"), \
                patch("Utils.exe_launch.game_is_steam_install",
                      return_value=True), \
                patch("Utils.exe_launch.effective_steam_id",
                      return_value="2868840"), \
                patch("Utils.exe_launch.load_exe_args", return_value=""), \
                patch("Utils.exe_launch.load_launch_options",
                      return_value=""), \
                patch("Utils.exe_launch.steam_launch_options_for_game",
                      return_value=generated_handoff) as steam_options, \
                patch("Utils.xdg.host_env",
                      return_value=dict(inherited_env)):
            launch_game(game, log_fn=messages.append)

        steam_options.assert_called_once()
        spawn.assert_called_once()
        command = spawn.call_args.args[0]
        assert command == [str(native_exe), "--default-argument"]
        assert "flatpak-spawn" not in command
        assert "launch" not in command
        assert all(spawn.call_args.kwargs["env"][key] == "2868840"
                   for key in inherited_env)
        assert any("ignoring Amethyst's Steam VFS handoff" in message
                   for message in messages)

        # Only Amethyst's generated handoff is special. Ordinary Steam
        # wrappers and their environment/arguments must still be preserved.
        normal_options = (
            "NORMAL_STEAM_WRAPPER=1 mangohud %command% --steam-suffix"
        )
        with patch("Utils.exe_launch.spawn_process_watched") as spawn, \
                patch("Utils.exe_launch.load_launch_mode",
                      return_value="none"), \
                patch("Utils.exe_launch.game_is_steam_install",
                      return_value=True), \
                patch("Utils.exe_launch.effective_steam_id",
                      return_value="2868840"), \
                patch("Utils.exe_launch.load_exe_args", return_value=""), \
                patch("Utils.exe_launch.load_launch_options",
                      return_value=""), \
                patch("Utils.exe_launch.steam_launch_options_for_game",
                      return_value=normal_options), \
                patch("Utils.xdg.host_env",
                      return_value=dict(inherited_env)):
            launch_game(game)
        normal_command = spawn.call_args.args[0]
        normal_exe_index = normal_command.index(str(native_exe))
        assert normal_command[:normal_exe_index] == ["mangohud"]
        assert normal_command[normal_exe_index + 1:] == [
            "--default-argument", "--steam-suffix",
        ]
        assert spawn.call_args.kwargs["env"]["NORMAL_STEAM_WRAPPER"] == "1"
    print("✓ native Steam fallback skips only Amethyst's own VFS handoff")


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


def test_cyberpunk_shadow_view() -> None:
    """Cyberpunk remaps archives and generates metadata only in its view."""
    with tempfile.TemporaryDirectory() as tmp:
        game = _FakeCyberpunkGame(Path(tmp))
        vanilla_exe = game.game / game.exe_name
        vanilla_exe.parent.mkdir(parents=True)
        vanilla_exe.write_text("vanilla cyberpunk", encoding="utf-8")

        physical_modlist = game.game / "archive/pc/mod/modlist.txt"
        physical_modlist.parent.mkdir(parents=True)
        physical_modlist.write_bytes(b"manual-vanilla.archive\r\n")

        high_archive = game.staging / "High" / "archive/pc/patch/high.archive"
        high_archive.parent.mkdir(parents=True)
        high_archive.write_text("high", encoding="utf-8")
        loose_archive = game.staging / "Loose" / "loose.archive"
        loose_archive.parent.mkdir(parents=True)
        loose_archive.write_text("loose", encoding="utf-8")
        loose_archive.with_suffix(".xl").write_text(
            "companion", encoding="utf-8")
        cet = (game.staging / "CET"
               / "bin/x64/plugins/cyber_engine_tweaks.asi")
        cet.parent.mkdir(parents=True)
        cet.write_text("cet", encoding="utf-8")
        redmod = game.staging / "REDmod" / "mods/TestRED/info.json"
        redmod.parent.mkdir(parents=True)
        redmod.write_text("{}", encoding="utf-8")
        overwrite_archive = (
            game.overwrite / "archive/pc/patch/overwrite.archive")
        overwrite_archive.parent.mkdir(parents=True)
        overwrite_archive.write_text("overwrite", encoding="utf-8")
        (game.root_folder / "version.dll").write_text(
            "root loader", encoding="utf-8")

        (game.profile / "modlist.txt").write_text(
            "+High\n+Loose\n+CET\n+REDmod\n", encoding="utf-8")
        game.filemap.write_text(
            "archive/pc/patch/overwrite.archive\t[Overwrite]\n"
            "archive/pc/patch/high.archive\tHigh\n"
            "loose.archive\tLoose\n"
            "loose.xl\tLoose\n"
            "bin/x64/plugins/cyber_engine_tweaks.asi\tCET\n"
            "mods/TestRED/info.json\tREDmod\n",
            encoding="utf-8",
        )

        for unsafe_dest in ("../escape/", "/tmp/escape/", "C:/escape/"):
            try:
                deploy_filemap(
                    game.filemap,
                    game.root / "unsafe-target",
                    game.staging,
                    path_remap={"archive/pc/patch/": unsafe_dest},
                )
            except RuntimeError as exc:
                assert "Unsafe deployment path remap" in str(exc)
            else:
                raise AssertionError(
                    f"unsafe VFS destination remap was accepted: {unsafe_dest}")

        logs: list[str] = []
        _deploy_custom_fixture(
            game, profile="default", log_fn=logs.append,
        )
        view = effective_shadow_root(game)
        assert view == game.profile / STATE_DIR_NAME / "view"
        manifest = game.profile / STATE_DIR_NAME / MANIFEST_NAME
        manifest_payload = json.loads(manifest.read_text(encoding="utf-8"))
        manifest_payload["view_root"] = str(game.game)
        manifest.write_text(json.dumps(manifest_payload), encoding="utf-8")
        try:
            effective_shadow_root(game)
        except RuntimeError as exc:
            assert "Unsafe profile VFS manifest view_root path" in str(exc)
        else:
            raise AssertionError("an out-of-state VFS shadow root was accepted")
        manifest_payload["view_root"] = str(view)
        root_trap = game.root / "manifest-root-trap"
        manifest_payload["root_layer"] = str(root_trap)
        manifest.write_text(json.dumps(manifest_payload), encoding="utf-8")
        generated_target = virtual_root_write_path(game, "generated.txt")
        assert generated_target == view / "generated.txt"
        assert root_trap not in generated_target.parents
        manifest_payload["root_layer"] = str(view)
        manifest.write_text(json.dumps(manifest_payload), encoding="utf-8")

        assert (view / "archive/pc/mod/overwrite.archive").read_text() == "overwrite"
        assert (view / "archive/pc/mod/high.archive").read_text() == "high"
        assert not (view / "archive/pc/patch/high.archive").exists()
        assert not (view / "archive/pc/patch/overwrite.archive").exists()
        assert (view / "archive/pc/mod/loose.archive").read_text() == "loose"
        assert (view / "archive/pc/mod/loose.xl").read_text() == "companion"
        assert (view / "bin/x64/plugins/cyber_engine_tweaks.asi").read_text() == "cet"
        assert (view / "version.dll").read_text() == "root loader"
        assert (view / "mods/TestRED/info.json").read_text() == "{}"
        assert (view / "archive/pc/mod/modlist.txt").read_bytes() == (
            b"overwrite.archive\r\nhigh.archive\r\nloose.archive\r\n"
        )

        # The real install and physical-deploy ownership sidecars remain
        # unchanged even though the view replaced an inherited hardlink.
        assert physical_modlist.read_bytes() == b"manual-vanilla.archive\r\n"
        assert not (game.game / "archive/pc/mod/high.archive").exists()
        assert not (game.game / "bin/x64/plugins/cyber_engine_tweaks.asi").exists()
        assert not (game.game / "version.dll").exists()
        assert not (game.profiles / "archive_modlist.state").exists()
        assert not (game.profiles / "archive_modlist_backup.txt").exists()

        assert game._deployed_redmods() == ["TestRED"]
        with patch.object(
            game, "_external_launch_missing_modded", return_value=None,
        ):
            game.post_deploy(log_fn=logs.append)
        assert any("1 mod(s) deployed under mods/" in line for line in logs)
        assert game.default_launch_args == ["-modded", "--launcher-skip"]
        assert game.default_launch_args_for_exe(
            "REDprelauncher.exe") == ["-modded"]
        assert game.framework_launch_exes == {
            "REDLauncher": "REDprelauncher.exe"}

        passthrough = game.get_vfs_passthrough_command([
            "lutris-runner", str(vanilla_exe),
        ])
        assert "-modded" in passthrough
        assert "--launcher-skip" in passthrough
        assert passthrough.count("-modded") == 1
        preconfigured = game.get_vfs_passthrough_command([
            str(vanilla_exe), "-modded",
        ])
        assert preconfigured.count("-modded") == 1
        assert "--launcher-skip" in preconfigured
        redlauncher = game.get_vfs_passthrough_command([
            "steam-wrapper", str(game.game / "REDprelauncher.exe"),
        ])
        assert "-modded" in redlauncher
        assert "--launcher-skip" not in redlauncher

        # Toggle-off restore still follows the published state and removes the
        # generated modlist with the shadow, never the physical original.
        game.set_vfs_enabled(False)
        game.restore(log_fn=logs.append)
        assert not has_deployment_state(game)
        assert physical_modlist.read_bytes() == b"manual-vanilla.archive\r\n"
        assert vanilla_exe.read_text(encoding="utf-8") == "vanilla cyberpunk"
        assert not (game.game / "version.dll").exists()

    # A migration can leave both kinds of state. Restore must unpublish the
    # private view and then consume the root-deploy journal and archive backup.
    with tempfile.TemporaryDirectory() as tmp:
        game = _FakeCyberpunkGame(Path(tmp))
        exe = game.game / game.exe_name
        exe.parent.mkdir(parents=True)
        exe.write_text("vanilla", encoding="utf-8")
        archive = game.staging / "Archive" / "archive/pc/mod/profile.archive"
        archive.parent.mkdir(parents=True)
        archive.write_text("profile", encoding="utf-8")
        (game.profile / "modlist.txt").write_text(
            "+Archive\n", encoding="utf-8")
        game.filemap.write_text(
            "archive/pc/mod/profile.archive\tArchive\n", encoding="utf-8")
        _deploy_custom_fixture(game, profile="default")

        physical_file = game.game / "r6/scripts/physical.reds"
        physical_file.parent.mkdir(parents=True)
        physical_file.write_text("legacy deploy", encoding="utf-8")
        (game.profiles / "filemap_deployed.txt").write_text(
            "r6/scripts/physical.reds", encoding="utf-8")
        physical_modlist = game.game / "archive/pc/mod/modlist.txt"
        physical_modlist.parent.mkdir(parents=True, exist_ok=True)
        generated = b"physical.archive\r\n"
        physical_modlist.write_bytes(generated)
        (game.profiles / "archive_modlist.state").write_bytes(generated)
        (game.profiles / "archive_modlist_backup.txt").write_bytes(
            b"original-physical-order\r\n")

        game.set_vfs_enabled(False)
        game.restore()
        assert not has_deployment_state(game)
        assert not physical_file.exists()
        assert physical_modlist.read_bytes() == b"original-physical-order\r\n"
        assert not (game.profiles / "filemap_deployed.txt").exists()
        assert not (game.profiles / "archive_modlist.state").exists()
        assert not (game.profiles / "archive_modlist_backup.txt").exists()

    # Root_Folder is applied after handler deploy in physical mode and thus
    # owns an explicit modlist.txt. VFS resolves the root payload earlier, so
    # its post-view generator must deliberately preserve that same winner.
    with tempfile.TemporaryDirectory() as tmp:
        game = _FakeCyberpunkGame(Path(tmp))
        exe = game.game / game.exe_name
        exe.parent.mkdir(parents=True)
        exe.write_text("vanilla", encoding="utf-8")
        archive = game.staging / "Archive" / "profile.archive"
        archive.parent.mkdir(parents=True)
        archive.write_text("archive", encoding="utf-8")
        root_modlist = game.root_folder / "archive/pc/mod/modlist.txt"
        root_modlist.parent.mkdir(parents=True)
        root_modlist.write_bytes(b"root-order.archive\r\n")
        overwrite_modlist = (
            game.overwrite / "archive/pc/mod/modlist.txt")
        overwrite_modlist.parent.mkdir(parents=True)
        overwrite_modlist.write_bytes(b"overwrite-order.archive\r\n")
        (game.profile / "modlist.txt").write_text(
            "+Archive\n", encoding="utf-8")
        game.filemap.write_text(
            "profile.archive\tArchive\n"
            "archive/pc/mod/modlist.txt\t[Overwrite]\n",
            encoding="utf-8")

        _deploy_custom_fixture(game, profile="default")
        view = effective_shadow_root(game)
        assert (view / "archive/pc/mod/modlist.txt").read_bytes() == (
            b"root-order.archive\r\n")
        assert not (game.game / "archive/pc/mod/modlist.txt").exists()
        game.restore()

    # A stale root map line whose source is missing never reached the view and
    # therefore cannot claim precedence over the generated load order.
    with tempfile.TemporaryDirectory() as tmp:
        game = _FakeCyberpunkGame(Path(tmp))
        exe = game.game / game.exe_name
        exe.parent.mkdir(parents=True)
        exe.write_text("vanilla", encoding="utf-8")
        archive = game.staging / "Archive" / "profile.archive"
        archive.parent.mkdir(parents=True)
        archive.write_text("archive", encoding="utf-8")
        (game.profile / "modlist.txt").write_text(
            "+Archive\n", encoding="utf-8")
        game.filemap.write_text(
            "profile.archive\tArchive\n", encoding="utf-8")
        (game.profiles / "filemap_root.txt").write_text(
            "archive/pc/mod/modlist.txt\tMissingRoot\n",
            encoding="utf-8",
        )

        _deploy_custom_fixture(game, profile="default")
        view = effective_shadow_root(game)
        assert (view / "archive/pc/mod/modlist.txt").read_bytes() == (
            b"profile.archive\r\n")
        game.restore()

    # A manifest is bookkeeping, never authority to scan/move arbitrary
    # directories. Corrupting view_root must make capture skip safely while
    # cleanup still removes only the profile's fixed managed state.
    with tempfile.TemporaryDirectory() as tmp:
        game = _FakeCyberpunkGame(Path(tmp))
        exe = game.game / game.exe_name
        exe.parent.mkdir(parents=True)
        exe.write_text("vanilla", encoding="utf-8")
        (game.profile / "modlist.txt").write_text("", encoding="utf-8")
        game.filemap.write_text("", encoding="utf-8")
        _deploy_custom_fixture(game, profile="default")

        state = game.profile / STATE_DIR_NAME
        manifest_path = state / MANIFEST_NAME
        corrupt = json.loads(manifest_path.read_text(encoding="utf-8"))
        corrupt["view_root"] = str(game.game)
        manifest_path.write_text(json.dumps(corrupt), encoding="utf-8")
        victim = game.game / "victim.txt"
        victim.write_text("must remain physical", encoding="utf-8")

        logs: list[str] = []
        game.restore(log_fn=logs.append)
        assert victim.read_text(encoding="utf-8") == "must remain physical"
        assert not (game.overwrite / "victim.txt").exists()
        assert not has_deployment_state(game)
        assert any("Unsafe profile VFS manifest view_root" in line
                   for line in logs)
    print("✓ Cyberpunk root VFS remap, metadata, REDmod scan, and restore")


def test_witcher3_shadow_view_and_script_merger() -> None:
    """Witcher routing and Script Merger operate entirely in the view."""
    with tempfile.TemporaryDirectory() as tmp:
        game = _FakeWitcher3Game(Path(tmp))
        vanilla_exe = game.game / game.exe_name
        vanilla_exe.parent.mkdir(parents=True)
        vanilla_exe.write_text("vanilla witcher", encoding="utf-8")

        menu_dir = game.game / "bin/config/r4game/user_config_matrix/pc"
        menu_dir.mkdir(parents=True)
        (menu_dir / "graphicsdx11.xml").write_text("dx11", encoding="utf-8")
        (menu_dir / "graphics.xml").write_text("dx12", encoding="utf-8")
        physical_dx11 = menu_dir / "dx11filelist.txt"
        physical_dx12 = menu_dir / "dx12filelist.txt"
        physical_dx11.write_text("physical-dx11;\n", encoding="utf-8")
        physical_dx12.write_text("physical-dx12;\n", encoding="utf-8")

        script = (
            game.staging / "ScriptPack" / "Full" /
            "modConflict/content/scripts/conflict.ws"
        )
        script.parent.mkdir(parents=True)
        script.write_text("mod script", encoding="utf-8")
        dlc = (
            game.staging / "DlcPack" / "Wrapper" /
            "dlcExample/content/content0.bundle"
        )
        dlc.parent.mkdir(parents=True)
        dlc.write_text("dlc", encoding="utf-8")
        menu_xml = (
            game.staging / "MenuPack" / "Full" /
            "bin/config/r4game/user_config_matrix/pc/modMenu.xml"
        )
        menu_xml.parent.mkdir(parents=True)
        menu_xml.write_text("menu", encoding="utf-8")

        # Explicit root payload is applied after generated metadata, matching
        # the physical pipeline's precedence.
        root_dx11 = (
            game.root_folder /
            "bin/config/r4game/user_config_matrix/pc/dx11filelist.txt"
        )
        root_dx11.parent.mkdir(parents=True)
        root_dx11.write_text("root-owned;\n", encoding="utf-8")

        (game.profile / "modlist.txt").write_text(
            "+ScriptPack\n+DlcPack\n+MenuPack\n", encoding="utf-8")
        game.filemap.write_text(
            "mods/modConflict/content/scripts/conflict.ws\tScriptPack\n"
            "dlc/dlcExample/content/content0.bundle\tDlcPack\n"
            "bin/config/r4game/user_config_matrix/pc/modMenu.xml\tMenuPack\n",
            encoding="utf-8",
        )

        logs: list[str] = []
        _deploy_custom_fixture(
            game, profile="default", log_fn=logs.append,
        )
        view = effective_shadow_root(game)
        assert (view / "mods/modConflict/content/scripts/conflict.ws").read_text() == "mod script"
        assert (view / "dlc/dlcExample/content/content0.bundle").read_text() == "dlc"
        assert (view / "bin/config/r4game/user_config_matrix/pc/modMenu.xml").read_text() == "menu"
        assert (view / "bin/config/r4game/user_config_matrix/pc/dx11filelist.txt").read_text() == "root-owned;\n"
        dx12 = (view / "bin/config/r4game/user_config_matrix/pc/dx12filelist.txt")
        assert "modMenu.xml;" in dx12.read_text(encoding="utf-8")
        assert "graphicsdx11.xml;" not in dx12.read_text(encoding="utf-8")

        # Host-side inventory validation must inspect the view, not the clean
        # physical game directory. The merger-created output is then rescued
        # into Merged_Mods before VFS cleanup.
        merged = view / "mods/mod0000_MergedFiles/content/scripts/merged.ws"
        merged.parent.mkdir(parents=True)
        merged.write_text("merged output", encoding="utf-8")
        from Utils.script_merger_inventory import (
            app_inventory_path,
            collateral_keys,
            missing_merge_sources,
            snapshot_inventory,
            snapshot_path,
        )
        inventory = app_inventory_path(game)
        inventory.parent.mkdir(parents=True, exist_ok=True)
        inventory.write_text(
            '<?xml version="1.0" encoding="utf-8"?>\n'
            '<MergeInventory><Merge>'
            '<RelativePath>merged.ws</RelativePath>'
            '<MergedModName>mod0000_MergedFiles</MergedModName>'
            '<IncludedMod>modConflict</IncludedMod>'
            '</Merge></MergeInventory>',
            encoding="utf-8",
        )
        assert missing_merge_sources(game) == []
        assert collateral_keys(game) == set()

        game.set_vfs_enabled(False)
        game.restore(log_fn=logs.append)
        staged_merge = (
            game.merged_mods_staging_dir() /
            "mods/mod0000_MergedFiles/content/scripts/merged.ws"
        )
        assert staged_merge.read_text(encoding="utf-8") == "merged output"
        assert snapshot_inventory(game, log_fn=logs.append)
        assert snapshot_path(game).is_file()
        assert not has_deployment_state(game)

        # Nothing from the VFS deploy or Script Merger escaped into the real
        # game tree, and generated filelists never rewrote vanilla files.
        assert vanilla_exe.read_text(encoding="utf-8") == "vanilla witcher"
        assert not (game.game / "mods/modConflict").exists()
        assert not (game.game / "mods/mod0000_MergedFiles").exists()
        assert physical_dx11.read_text(encoding="utf-8") == "physical-dx11;\n"
        assert physical_dx12.read_text(encoding="utf-8") == "physical-dx12;\n"

    # Migration can leave a physical Witcher deployment underneath a newly
    # published view. Restore must clean both rather than treating VFS state
    # as authoritative and returning early.
    with tempfile.TemporaryDirectory() as tmp:
        game = _FakeWitcher3Game(Path(tmp))
        source = game.staging / "Physical" / "modPhysical/content/a.txt"
        source.parent.mkdir(parents=True)
        source.write_text("physical mod", encoding="utf-8")
        (game.profile / "modlist.txt").write_text(
            "+Physical\n", encoding="utf-8")
        game.filemap.write_text(
            "mods/modPhysical/content/a.txt\tPhysical\n",
            encoding="utf-8",
        )
        game.set_vfs_enabled(False)
        game.deploy(profile="default", mode=LinkMode.HARDLINK)
        physical_target = game.game / "mods/modPhysical/content/a.txt"
        assert physical_target.is_file()

        game.set_vfs_enabled(True)
        _deploy_custom_fixture(game, profile="default")
        assert has_deployment_state(game)
        game.set_vfs_enabled(False)
        game.restore()
        assert not has_deployment_state(game)
        assert not physical_target.exists()
    print("✓ Witcher 3 routed VFS, private filelists, and Script Merger rescue")


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

        # Legacy engines such as Skyrim must retain the configured install as
        # their visible working path. Deep loose assets can be below MAX_PATH
        # there but cross it when rooted below `.amethyst-vfs/view`. The
        # handler opt-in deliberately bypasses direct runtime retargeting and
        # restores the original bind-at-game-root launch shape.
        game.vfs_bind_launch_at_game_root = True
        bound_env = os.environ.copy()
        bound_env["STEAM_COMPAT_INSTALL_PATH"] = str(canonical_game)
        with patch("Utils.vfs.overlay._bubblewrap_status",
                   return_value=(True, "")), \
                patch("Utils.vfs.overlay._bubblewrap_binary",
                      return_value="/usr/bin/bwrap"):
            bound = wrap_command(game, original, env=bound_env)
        assert Path(bound[0]).name == "bwrap"
        bind_index = bound.index("--bind")
        assert bound[bind_index + 1:bind_index + 3] == [
            str(view), str(canonical_game.resolve()),
        ]
        assert str(real_exe) in bound
        assert str(shadow_exe) not in bound
        assert bound_env["STEAM_COMPAT_INSTALL_PATH"] == str(canonical_game)
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
    test_shadow_capture_survives_configured_path_change()
    test_failed_post_view_hook_never_promotes_partial_output()
    test_vfs_root_payload_preserves_physical_recovery()
    test_shared_bethesda_hooks()
    test_wizard_tools_use_vfs_and_xedit_edits_persist()
    test_generic_mod_data_directory()
    test_vfs_cleanup_failure_remains_discoverable()
    test_symlinked_vfs_state_root_is_never_followed()
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
    test_native_bepinex_shadow_launch()
    test_native_none_launch_steam_context()
    test_native_steam_client_lifecycle()
    test_native_vfs_flatpak_forwards_launch_environment()
    test_native_steam_handoff_fallback_is_not_recursive()
    test_stardew_shadow_view()
    test_cyberpunk_shadow_view()
    test_witcher3_shadow_view_and_script_merger()
    test_vfs_as_deploy_method()
    test_deploy_pipeline_stops_on_incomplete_restore()
    test_flatpak_host_wrap()
    test_umu_uses_shadow_directly()
    test_steam_runtime_uses_shadow_directly()
    test_launcher_aware_handoffs()
    print("All profile VFS self-tests passed.")


if __name__ == "__main__":
    main()
