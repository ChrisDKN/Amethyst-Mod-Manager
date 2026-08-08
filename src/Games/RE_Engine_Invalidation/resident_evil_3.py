"""
resident_evil_3.py
Resident Evil 3 Remake (2020) game handler.
"""

from Games.RE_Engine_Invalidation.resident_evil_village import ResidentEvilVillage


class ResidentEvil3(ResidentEvilVillage):
    """Resident Evil 3 Remake (2020).

    Uses natives/STM/ instead of natives/x64/ - mods ship with x64 paths
    but must be deployed to STM.  Skipped on the dx11_non-rt beta branch.
    """

    _rt_path_remap = {"natives/x64/": "natives/STM/"}
    _rt_ext_remap = {".tex.10": ".tex.34"}

    @property
    def name(self) -> str:
        return "Resident Evil 3"

    @property
    def game_id(self) -> str:
        return "resident_evil_3"

    @property
    def exe_name(self) -> str:
        return "re3.exe"

    @property
    def steam_id(self) -> str:
        return "952060"

    @property
    def nexus_game_domain(self) -> str:
        return "residentevil32020"
