"""Dyson Sphere Program game handler."""

from Games.BepInEx.bepinex_base import BepInExBase


class DysonSphereProgram(BepInExBase):
    @property
    def name(self) -> str:
        return "Dyson Sphere Program"

    @property
    def game_id(self) -> str:
        return "dysonsphereprogram"

    @property
    def exe_name(self) -> str:
        return "DSPGAME.exe"

    @property
    def steam_id(self) -> str:
        return "1366540"

    @property
    def nexus_game_domain(self) -> str:
        return "dysonsphereprogram"

    @property
    def thunderstore_community(self) -> str:
        return "dyson-sphere-program"
