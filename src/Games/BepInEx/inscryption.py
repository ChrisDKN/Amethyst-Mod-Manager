"""Inscryption game handler."""

from Games.BepInEx.bepinex_base import BepInExBase


class Inscryption(BepInExBase):
    @property
    def name(self) -> str:
        return "Inscryption"

    @property
    def game_id(self) -> str:
        return "inscryption"

    @property
    def exe_name(self) -> str:
        return "Inscryption.exe"

    @property
    def exe_name_alts(self) -> list[str]:
        return ["Inscryption.x86_64"]

    @property
    def steam_id(self) -> str:
        return "1092790"

    @property
    def nexus_game_domain(self) -> str:
        return ""

    @property
    def thunderstore_community(self) -> str:
        return "inscryption"

    @property
    def extra_mod_folder_strip_prefixes(self) -> set[str]:
        return {"BepInExPack_Inscryption"}
