"""
monster_hunter_rise.py
Game handler for Monster Hunter Rise.
"""

from Games.RE_Engine_LooseLoading.resident_evil_requiem import ResidentEvilRequiem


class MonsterHunterRise(ResidentEvilRequiem):

    @property
    def name(self) -> str:
        return "Monster Hunter Rise"

    @property
    def game_id(self) -> str:
        return "monster_hunter_rise"

    @property
    def exe_name(self) -> str:
        return "MonsterHunterRise.exe"

    @property
    def steam_id(self) -> str:
        return "1446780"

    @property
    def nexus_game_domain(self) -> str:
        return "monsterhunterrise"

    @property
    def collections_disabled(self) -> bool:
        return False

    @property
    def custom_routing_rules(self) -> list:
        from Utils.deploy import CustomRule
        return super().custom_routing_rules + [
            CustomRule(
                dest="reframework/quests",
                extensions=[".json"],
                flatten=True,
                loose_only=True,
            ),
            CustomRule(
                dest="reframework",
                folders=["quests"],
                flatten=True,
            ),
        ]

    # Profile Groups: disabled for now — only Stardew Valley currently opts
    # in (see base_game.py's profile_groups_supported docstring) pending
    # wider review of the virtual-merge rework. Uncomment to enable once
    # this game's deploy path has been reviewed/tested.
    # @property
    # def profile_groups_supported(self) -> bool:
    #     return True
