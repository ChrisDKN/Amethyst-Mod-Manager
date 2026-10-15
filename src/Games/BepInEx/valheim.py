"""Valheim game handler."""

import stat

from Games.BepInEx.bepinex_base import BepInExBase
from Utils.deployment import LinkMode


class Valheim(BepInExBase):
    @property
    def vfs_native_launcher_names(self) -> tuple[str, ...]:
        # The Thunderstore Valheim pack supplies this wrapper and documents it
        # as `./start_game_bepinex.sh %command%`.
        return ("start_game_bepinex.sh", "run_bepinex.sh")

    @property
    def name(self) -> str:
        return "Valheim"

    @property
    def game_id(self) -> str:
        return "Valheim"

    @property
    def exe_name(self) -> str:
        return "valheim.exe"

    @property
    def exe_name_alts(self) -> list[str]:
        return ["valheim.x86_64"]

    @property
    def steam_id(self) -> str:
        return "892970"

    @property
    def nexus_game_domain(self) -> str:
        return "valheim"

    @property
    def thunderstore_community(self) -> str:
        return "valheim"

    @property
    def extra_mod_folder_strip_prefixes(self) -> set[str]:
        return {"BepInExPack_Valheim"}

    @property
    def default_deploy_mode(self) -> str:
        return "symlink"

    def deploy(self, log_fn=None, mode: LinkMode = LinkMode.HARDLINK,
                profile: str = "default", progress_fn=None) -> None:
        """Deploy staged mods into BepInEx/Plugins/ for Valheim, with extra steps."""
        super().deploy(log_fn=log_fn, mode=mode, profile=profile, progress_fn=progress_fn)

        """Run after all deployment steps, including Root_Folder moves."""
        _log = log_fn or (lambda _: None)
        if self.vfs_launch_enabled and self._vfs_native_game_exe() is not None:
            _log(
                "VFS launch: Amethyst automatically wraps Valheim with "
                "start_game_bepinex.sh. Launch through Amethyst or the "
                "launcher-specific VFS command."
            )
            return
        if self.vfs_launch_enabled:
            _log(
                "VFS launch: launch Valheim through Amethyst or the "
                "launcher-specific VFS command so the private game view is "
                "active."
            )
            return
        game_path = self.get_game_path()
        root_folder = self.get_effective_root_folder_path()
        candidates = []
        if game_path is not None:
            candidates.append(game_path / "start_game_bepinex.sh")
        candidates.append(root_folder / "start_game_bepinex.sh")
        found = False
        for launcher in candidates:
            if launcher.exists():
                current_mode = launcher.stat().st_mode
                launcher.chmod(current_mode | stat.S_IXUSR)
                _log(f"Set executable bit (u+x) on {launcher}.")
                found = True
                break
        if not found:
            _log("Warning: start_game_bepinex.sh not found in game folder or Root_Folder; skipping chmod.")

        if found and self.get_prefix_path() is None:
            _log(
                "Amethyst's Play button runs native Valheim through BepInEx "
                "automatically. To launch through Steam directly, set this "
                "Steam launch option:\n"
                "    ./start_game_bepinex.sh %command%\n"
                "Add it in Steam's Valheim Properties > Launch Options."
            )
        else:
            _log("Play will use Valheim's normal launch route.")
