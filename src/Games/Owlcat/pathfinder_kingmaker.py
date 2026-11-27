"""Pathfinder: Kingmaker handler (UMM mods only)."""

from Games.Owlcat.owlcat_base import OwlcatBase


class PathfinderKingmaker(OwlcatBase):
    game_name = "Pathfinder: Kingmaker"
    game_id_value = "pathfinder_kingmaker"
    exe = "Kingmaker.exe"
    steam_app_id = "640820"
    nexus_domain = "pathfinderkingmaker"
    locallow_dir_name = "Pathfinder Kingmaker"
    supports_template_mods = False
