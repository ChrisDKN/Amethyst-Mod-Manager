"""
pragmata.py
Pragmata game handler.
"""

from Games.RE_Engine_LooseLoading.resident_evil_requiem import ResidentEvilRequiem


class Pragmata(ResidentEvilRequiem):

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

    # Profile Groups: disabled for now — only Stardew Valley currently opts
    # in (see base_game.py's profile_groups_supported docstring) pending
    # wider review of the virtual-merge rework. Uncomment to enable once
    # this game's deploy path has been reviewed/tested.
    # @property
    # def profile_groups_supported(self) -> bool:
    #     return True
