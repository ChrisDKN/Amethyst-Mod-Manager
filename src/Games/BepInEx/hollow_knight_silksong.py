"""Hollow Knight: Silksong game handler."""

from Games.BepInEx.bepinex_base import BepInExBase


class HNSS(BepInExBase):
    @property
    def name(self) -> str:
        return "Hollow Knight: Silksong"

    @property
    def game_id(self) -> str:
        return "Hollow_Knight_Silksong"

    @property
    def exe_name(self) -> str:
        return "Hollow Knight Silksong.exe"

    @property
    def steam_id(self) -> str:
        return "1030300"

    @property
    def nexus_game_domain(self) -> str:
        return "hollowknightsilksong"

    @property
    def thunderstore_community(self) -> str:
        return "hollow-knight-silksong"

    @property
    def exe_name_alts(self) -> list[str]:
        return ["Hollow Knight Silksong"]

    @property
    def default_deploy_mode(self) -> str:
        return "symlink"
