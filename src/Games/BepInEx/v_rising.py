"""V Rising handler."""

from Games.BepInEx.bepinex_base import BepInExBase


class VRising(BepInExBase):
    @property
    def name(self) -> str:
        return "V Rising"

    @property
    def game_id(self) -> str:
        return "v_rising"

    @property
    def exe_name(self) -> str:
        return "VRising.exe"

    @property
    def steam_id(self) -> str:
        return "1604030"

    @property
    def nexus_game_domain(self) -> str:
        return "vrising"

    @property
    def thunderstore_community(self) -> str:
        return "v-rising"

    @property
    def extra_mod_folder_strip_prefixes(self) -> set[str]:
        return {"BepInExPack_V_Rising"}