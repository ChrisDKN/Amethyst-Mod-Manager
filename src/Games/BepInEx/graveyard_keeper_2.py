"""Graveyard Keeper 2 game handler."""

from Games.BepInEx.bepinex_base import BepInExBase


class GraveyardKeeper2(BepInExBase):
    @property
    def name(self) -> str:
        return "Graveyard Keeper 2"

    @property
    def game_id(self) -> str:
        return "graveyard_keeper_2"

    @property
    def exe_name(self) -> str:
        return "GraveyardKeeper2.exe"

    @property
    def steam_id(self) -> str:
        return "1092790"

    @property
    def nexus_game_domain(self) -> str:
        return "graveyardkeeper2"

    @property
    def thunderstore_community(self) -> str:
        return "graveyard-keeper-2"
