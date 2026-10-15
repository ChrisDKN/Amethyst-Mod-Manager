"""Lethal Company game handler."""

from Games.BepInEx.bepinex_base import BepInExBase


class Lethal_Company(BepInExBase):

    @property
    def name(self) -> str:
        return "Lethal Company"

    @property
    def game_id(self) -> str:
        return "Lethal_Company"

    @property
    def exe_name(self) -> str:
        return "Lethal Company.exe"

    @property
    def steam_id(self) -> str:
        return "1966720"

    @property
    def nexus_game_domain(self) -> str:
        return "lethalcompany"

    @property
    def thunderstore_community(self) -> str:
        return "lethal-company"

    @property
    def default_deploy_mode(self) -> str:
        return "symlink"
