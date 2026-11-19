"""Warhammer 40,000: Rogue Trader handler (built-in UMM + Owlcat Template mods)."""

from Games.Owlcat.owlcat_base import OwlcatBase


class RogueTrader(OwlcatBase):
    game_name = "Warhammer 40,000: Rogue Trader"
    game_id_value = "rogue_trader"
    exe = "WH40KRT.exe"
    steam_app_id = "2186680"
    nexus_domain = "warhammer40kroguetrader"
    locallow_dir_name = "Warhammer 40000 Rogue Trader"
    umm_builtin = True
