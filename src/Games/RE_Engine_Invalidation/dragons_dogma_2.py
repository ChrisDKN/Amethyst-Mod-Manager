"""
dragons_dogma_2.py
Dragons Dogma 2 game handler.
"""

from Games.RE_Engine_Invalidation.resident_evil_village import ResidentEvilVillage


class DragonsDogma2(ResidentEvilVillage):

    @property
    def name(self) -> str:
        return "Dragons Dogma 2"

    @property
    def game_id(self) -> str:
        return "dragons_dogma_2"

    @property
    def exe_name(self) -> str:
        return "DD2.exe"

    @property
    def steam_id(self) -> str:
        return "2054970"

    @property
    def nexus_game_domain(self) -> str:
        return "dragonsdogma2"

    # Profile Groups: disabled for now — only Stardew Valley currently opts
    # in (see base_game.py's profile_groups_supported docstring) pending
    # wider review of the virtual-merge rework. Uncomment to enable once
    # this game's deploy path has been reviewed/tested.
    # @property
    # def profile_groups_supported(self) -> bool:
    #     return True
