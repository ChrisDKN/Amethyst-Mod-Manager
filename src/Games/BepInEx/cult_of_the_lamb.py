"""Cult of the Lamb game handler."""

from Games.BepInEx.bepinex_base import BepInExBase


class CultOfTheLamb(BepInExBase):
    @property
    def name(self) -> str:
        return "Cult of the Lamb"

    @property
    def game_id(self) -> str:
        return "cult_of_the_lamb"

    @property
    def exe_name(self) -> str:
        return "Cult Of The Lamb.exe"

    @property
    def steam_id(self) -> str:
        return "1313140"

    @property
    def nexus_game_domain(self) -> str:
        return "cultofthelamb"

    @property
    def thunderstore_community(self) -> str:
        return "cult-of-the-lamb"

    @property
    def extra_mod_folder_strip_prefixes(self) -> set[str]:
        return {"BepInExPack_CultOfTheLamb"}
