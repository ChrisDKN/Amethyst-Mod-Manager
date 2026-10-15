"""Subnautica: Below Zero game handler."""

from Games.BepInEx.bepinex_base import BepInExBase


class Subnautica_Below_Zero(BepInExBase):

    @property
    def name(self) -> str:
        return "Subnautica: Below Zero"

    @property
    def game_id(self) -> str:
        return "Subnautica_Below_Zero"

    @property
    def exe_name(self) -> str:
        return "SubnauticaZero.exe"

    @property
    def steam_id(self) -> str:
        return "848450"

    @property
    def nexus_game_domain(self) -> str:
        return "subnauticabelowzero"

    @property
    def thunderstore_community(self) -> str:
        return "subnautica-below-zero"
