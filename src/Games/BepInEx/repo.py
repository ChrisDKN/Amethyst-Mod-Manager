"""R.E.P.O game handler."""

from Games.BepInEx.bepinex_base import BepInExBase


class repo(BepInExBase):
    @property
    def name(self) -> str:
        return "R.E.P.O"

    @property
    def game_id(self) -> str:
        return "repo"

    @property
    def exe_name(self) -> str:
        return "REPO.exe"

    @property
    def steam_id(self) -> str:
        return "3241660"

    @property
    def nexus_game_domain(self) -> str:
        return "repo"

    @property
    def thunderstore_community(self) -> str:
        return "repo"
