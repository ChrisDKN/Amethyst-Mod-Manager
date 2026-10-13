"""Risk of Rain 2 game handler."""

from Games.BepInEx.bepinex_base import BepInExBase


class ROR2(BepInExBase):
    @property
    def name(self) -> str:
        return "Risk of Rain 2"

    @property
    def game_id(self) -> str:
        return "riskofrain2"

    @property
    def exe_name(self) -> str:
        return "Risk of Rain 2.exe"

    @property
    def steam_id(self) -> str:
        return "632360"

    @property
    def nexus_game_domain(self) -> str:
        return ""

    @property
    def thunderstore_community(self) -> str:
        return "riskofrain2"
