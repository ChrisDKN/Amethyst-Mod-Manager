"""Subnautica game handler."""

from Games.BepInEx.bepinex_base import BepInExBase


class Subnautica(BepInExBase):
    @property
    def name(self) -> str:
        return "Subnautica"

    @property
    def game_id(self) -> str:
        return "Subnautica"

    @property
    def exe_name(self) -> str:
        return "Subnautica.exe"

    @property
    def steam_id(self) -> str:
        return "264710"

    @property
    def nexus_game_domain(self) -> str:
        return "subnautica"

    @property
    def thunderstore_community(self) -> str:
        return "subnautica"
