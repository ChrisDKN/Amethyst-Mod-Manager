"""TCG Card Shop Simulator game handler."""

from Games.BepInEx.bepinex_base import BepInExBase


class TCG_Card_Shop_Simulator(BepInExBase):

    @property
    def name(self) -> str:
        return "TCG Card Shop Simulator"

    @property
    def game_id(self) -> str:
        return "TCG_Card_Shop_Simulator"

    @property
    def exe_name(self) -> str:
        return "Card Shop Simulator.exe"

    @property
    def steam_id(self) -> str:
        return "3070070"

    @property
    def nexus_game_domain(self) -> str:
        return "tcgcardshopsimulator"

    @property
    def thunderstore_community(self) -> str:
        return "tcg-card-shop-simulator"

    @property
    def default_deploy_mode(self) -> str:
        return "symlink"
