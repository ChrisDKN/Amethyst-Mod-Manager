"""Supermarket Simulator game handler."""

from Games.BepInEx.bepinex_base import BepInExBase


class SupermarketSimulator(BepInExBase):
    @property
    def name(self) -> str:
        return "Supermarket Simulator"

    @property
    def game_id(self) -> str:
        return "Supermarket_Simulator"

    @property
    def exe_name(self) -> str:
        return "Supermarket Simulator.exe"

    @property
    def steam_id(self) -> str:
        return "2670630"

    @property
    def nexus_game_domain(self) -> str:
        return "supermarketsimulator"

    @property
    def thunderstore_community(self) -> str:
        return ""
