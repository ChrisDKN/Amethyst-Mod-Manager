"""Pragmata game handler."""

from Games.RE_Engine_LooseLoading.re_engine_loose_loading_base import REEngineLooseLoadingBase


class Pragmata(REEngineLooseLoadingBase):

    @property
    def name(self) -> str:
        return "Pragmata"

    @property
    def game_id(self) -> str:
        return "Pragmata"

    @property
    def exe_name(self) -> str:
        return "PRAGMATA.exe"

    @property
    def steam_id(self) -> str:
        return "3357650"

    @property
    def nexus_game_domain(self) -> str:
        return "pragmata"
