"""
resident_evil_4.py
Resident Evil 4 Remake (2023) game handler.
"""

from Games.RE_Engine_Invalidation.resident_evil_village import ResidentEvilVillage


class ResidentEvil4(ResidentEvilVillage):
    """Resident Evil 4 Remake (2023) — identical workflow to RE Village."""

    @property
    def name(self) -> str:
        return "Resident Evil 4"

    @property
    def game_id(self) -> str:
        return "resident_evil_4"

    @property
    def exe_name(self) -> str:
        return "re4.exe"

    @property
    def steam_id(self) -> str:
        return "2050650"

    @property
    def nexus_game_domain(self) -> str:
        return "residentevil42023"

    # Profile Groups: disabled for now — only Stardew Valley currently opts
    # in (see base_game.py's profile_groups_supported docstring) pending
    # wider review of the virtual-merge rework. Uncomment to enable once
    # this game's deploy path has been reviewed/tested.
    # @property
    # def profile_groups_supported(self) -> bool:
    #     return True
