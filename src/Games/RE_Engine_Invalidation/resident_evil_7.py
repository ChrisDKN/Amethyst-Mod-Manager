"""
resident_evil_7.py
Resident Evil 7: Biohazard game handler.
"""

from Games.RE_Engine_Invalidation.resident_evil_village import ResidentEvilVillage


class ResidentEvil7(ResidentEvilVillage):
    """Resident Evil 7: Biohazard — identical workflow to RE Village."""

    @property
    def name(self) -> str:
        return "Resident Evil 7"

    @property
    def game_id(self) -> str:
        return "resident_evil_7"

    @property
    def exe_name(self) -> str:
        return "re7.exe"

    @property
    def steam_id(self) -> str:
        return "418370"

    @property
    def nexus_game_domain(self) -> str:
        return "residentevil7"

    @property
    def mod_deploy_path_remap(self) -> dict[str, str]:
        return {"natives/x64/": "natives/STM/"}

    @property
    def pak_hash_extension_remap(self) -> dict[str, str]:
        return {".tex.10": ".tex.34"}
