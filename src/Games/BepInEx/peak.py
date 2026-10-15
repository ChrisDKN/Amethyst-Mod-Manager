"""Peak game handler."""

from Games.BepInEx.bepinex_base import BepInExBase


class Peak(BepInExBase):
    @property
    def name(self) -> str:
        return "Peak"

    @property
    def game_id(self) -> str:
        return "peak"

    @property
    def exe_name(self) -> str:
        return "PEAK.exe"

    @property
    def steam_id(self) -> str:
        return "3527290"

    @property
    def nexus_game_domain(self) -> str:
        return "peak"

    @property
    def thunderstore_community(self) -> str:
        return "peak"

    @property
    def extra_mod_folder_strip_prefixes(self) -> set[str]:
        return {"BepInExPack_Peak"}
