"""Resident Evil Requiem game handler."""

from Games.RE_Engine_LooseLoading.re_engine_loose_loading_base import REEngineLooseLoadingBase


class ResidentEvilRequiem(REEngineLooseLoadingBase):
    @property
    def name(self) -> str:
        return "Resident Evil Requiem"

    @property
    def game_id(self) -> str:
        return "resident_evil_requiem"

    @property
    def exe_name(self) -> str:
        return "re9.exe"

    @property
    def steam_id(self) -> str:
        return "3764200"

    @property
    def nexus_game_domain(self) -> str:
        return "residentevilrequiem"
