from importlib import import_module
from pathlib import Path

from Utils.deployment import CustomRule, LinkMode

SimsBase = import_module("Games.The Sims.sims_base").SimsBase

_RESOURCE_CFG = "".join(
    f"Priority {priority}\n" + "".join(
        f"PackedFile {folder}/{'*/' * depth}*.package\n" for depth in range(5)
    )
    for folder, priority in (("Packages", 500), ("Overrides", 1000))
)
_CACHE_FILENAMES = {
    "caspartcache.package", "compositorcache.package", "scriptcache.package",
    "simcompositorcache.package", "socialcache.package",
}


class _LauncherInstallRequired(ValueError):
    install_cancelled = True


class TheSims3(SimsBase):
    @property
    def name(self) -> str:
        return "The Sims 3"

    @property
    def game_id(self) -> str:
        return "the_sims_3"

    @property
    def steam_id(self) -> str:
        return "47890"

    @property
    def nexus_game_domain(self) -> str:
        return "thesims3"

    @property
    def exe_name(self) -> str:
        return "Game/Bin/TS3W.exe"

    @property
    def exe_name_alts(self) -> list[str]:
        return ["Game/Bin/TS3.exe", "Game/Bin/Sims3Launcher.exe",
                "Game/Bin/Sims3LauncherW.exe"]

    @property
    def preferred_launch_exe(self) -> str:
        if self._game_path is not None:
            try:
                version = (self._game_path / "Game/Bin/skuversion.txt").read_text(
                    encoding="utf-8-sig", errors="replace")
                for line in version.splitlines():
                    key, _, value = line.partition("=")
                    if key.strip().lower() == "gameversion" and value.strip().startswith("1.69."):
                        return "Game/Bin/TS3.exe"
            except OSError:
                pass
        return ""

    @property
    def direct_launch_exes(self) -> list[str]:
        return ["Game/Bin/TS3W.exe", "Game/Bin/TS3.exe"]

    @property
    def reshade_dll(self) -> str:
        return "d3d9.dll"

    @property
    def reshade_arch(self) -> int:
        return 32

    @property
    def wine_dll_overrides(self) -> dict[str, str]:
        return {"wininet": "native,builtin"}

    @property
    def mods_subpath(self) -> Path:
        return self.user_subpath / "Mods/Packages"

    @property
    def mod_required_top_level_folders(self) -> set[str]:
        return {"Mods", "Packages", "Overrides", "SavedSims", "Library", "Game", "Bin"}

    @property
    def mod_folder_strip_prefixes(self) -> set[str]:
        return {"Mods", "Packages"}

    @property
    def mod_folder_strip_prefixes_post(self) -> set[str]:
        return {"Mods", "Packages"}

    @property
    def mod_required_file_types(self) -> set[str]:
        return {".package", ".sim", ".dll", ".asi", ".sgr", ".sims3pack"}

    @property
    def mod_install_extensions(self) -> set[str]:
        return {".package", ".sim", ".dll", ".asi", ".ini", ".cfg", ".txt", ".sgr"}

    @property
    def conflict_ignore_filenames(self) -> set[str]:
        return {"*.sims3pack", "resource.cfg", "*readme*", "*read me*", "*read_me*"}

    @property
    def custom_routing_rules(self) -> list[CustomRule]:
        return [
            CustomRule(
                rule_id="the_sims_3:saved_sims",
                dest=str(self.user_subpath / "SavedSims"),
                extensions=[".sim"], flatten=True, to_prefix=True,
            ),
            CustomRule(
                rule_id="the_sims_3:library",
                dest=str(self.user_subpath / "Library"), folders=["Library"],
                flatten_contents=True, to_prefix=True,
            ),
            CustomRule(
                rule_id="the_sims_3:overrides",
                dest=str(self.user_subpath / "Mods"), folders=["Overrides"],
                flatten=True, to_prefix=True,
            ),
            CustomRule(
                rule_id="the_sims_3:graphics", dest="Game/Bin",
                filenames=["GraphicsRules.sgr", "GraphicsCards.sgr"], flatten=True,
            ),
            CustomRule(
                rule_id="the_sims_3:game_bin", dest="",
                folders=["Game/Bin"], flatten=True,
            ),
            CustomRule(
                rule_id="the_sims_3:bin", dest="Game",
                folders=["Bin"], flatten=True,
            ),
            CustomRule(
                rule_id="the_sims_3:binaries", dest="Game/Bin",
                extensions=[".dll", ".asi"],
                companion_extensions=[".ini", ".cfg", ".txt"], flatten=True,
            ),
        ]

    @property
    def additional_install_logic(self) -> list:
        return [self._check_sims3packs]

    @staticmethod
    def _check_sims3packs(dest_root: Path, mod_name: str, log_fn) -> None:
        files = [p for p in dest_root.rglob("*") if p.is_file()]
        if not any(p.suffix.lower() == ".sims3pack" for p in files):
            return
        message = (
            "Sims3Pack files require installation through The Sims 3 Launcher; "
            "Amethyst does not deploy or uninstall them."
        )
        log_fn(f"{mod_name}: {message}")
        if not any(p.suffix.lower() in {".package", ".sim", ".dll", ".asi", ".sgr"}
                   for p in files):
            raise _LauncherInstallRequired(message)

    def _clear_mod_caches(self, log_fn=None) -> None:
        if self._prefix_path is None:
            return
        log = log_fn or (lambda _: None)
        user_dir = self._prefix_path / self.user_subpath
        try:
            paths = list(user_dir.iterdir())
        except FileNotFoundError:
            return
        except OSError as exc:
            log(f"WARN: Could not inspect Sims 3 caches: {exc}")
            return
        removed = 0
        for path in paths:
            if path.name.lower() not in _CACHE_FILENAMES:
                continue
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            except OSError as exc:
                log(f"WARN: Could not clear Sims 3 cache {path.name}: {exc}")
            else:
                removed += 1
        if removed:
            log(f"Cleared {removed} Sims 3 cache file(s).")

    def deploy(self, log_fn=None, mode: LinkMode = LinkMode.HARDLINK,
               profile: str = "default", progress_fn=None) -> None:
        super().deploy(log_fn=log_fn, mode=mode, profile=profile, progress_fn=progress_fn)
        self._clear_mod_caches(log_fn)

    def restore(self, log_fn=None, progress_fn=None) -> None:
        super().restore(log_fn=log_fn, progress_fn=progress_fn)
        self._clear_mod_caches(log_fn)

    def get_hardlink_deploy_targets(self) -> list[tuple[str, Path | None]]:
        return [*super().get_hardlink_deploy_targets(), ("Game directory", self._game_path)]

    def prepare_mod_directory(self, mods_dir: Path, log_fn) -> None:
        if self._game_path is None:
            raise RuntimeError("Game path is not configured.")
        super().prepare_mod_directory(mods_dir, log_fn)
        (mods_dir.parent / "Overrides").mkdir(exist_ok=True)
        resource = next(
            (p for p in mods_dir.parent.iterdir() if p.name.lower() == "resource.cfg"),
            mods_dir.parent / "Resource.cfg",
        )
        try:
            with resource.open("x", encoding="utf-8") as stream:
                stream.write(_RESOURCE_CFG)
        except FileExistsError:
            log_fn(f"Keeping existing mod framework: {resource}")
        else:
            log_fn(f"Created mod framework: {resource}")
