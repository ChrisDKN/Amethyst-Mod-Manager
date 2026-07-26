"""
resident_evil_2.py
Resident Evil 2 Remake (2019) game handler.
"""

from Games.RE_Engine_Invalidation.resident_evil_village import ResidentEvilVillage


class ResidentEvil2(ResidentEvilVillage):
    """Resident Evil 2 Remake (2019).

    Uses natives/STM/ instead of natives/x64/ — mods ship with x64 paths
    but must be deployed to STM.
    """

    @property
    def name(self) -> str:
        return "Resident Evil 2"

    @property
    def game_id(self) -> str:
        return "resident_evil_2"

    @property
    def exe_name(self) -> str:
        return "re2.exe"

    @property
    def steam_id(self) -> str:
        return "883710"

    @property
    def nexus_game_domain(self) -> str:
        return "residentevil22019"

    @property
    def mod_deploy_path_remap(self) -> dict[str, str]:
        return {"natives/x64/": "natives/STM/"}

    @property
    def pak_hash_extension_remap(self) -> dict[str, str]:
        return {".tex.10": ".tex.34"}
