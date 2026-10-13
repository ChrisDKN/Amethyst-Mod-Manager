"""Resident Evil Village (RE8) game handler."""

from Games.RE_Engine_Invalidation.re_engine_invalidation_base import REEngineInvalidationBase


class ResidentEvilVillage(REEngineInvalidationBase):
    @property
    def name(self) -> str:
        return "Resident Evil Village"

    @property
    def game_id(self) -> str:
        return "resident_evil_village"

    @property
    def exe_name(self) -> str:
        return "re8.exe"

    @property
    def steam_id(self) -> str:
        return "1196590"

    @property
    def nexus_game_domain(self) -> str:
        return "residentevilvillage"
