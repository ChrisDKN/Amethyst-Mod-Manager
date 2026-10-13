"""Monster Hunter Rise game handler."""

from Games.RE_Engine_LooseLoading.re_engine_loose_loading_base import REEngineLooseLoadingBase


class MonsterHunterRise(REEngineLooseLoadingBase):

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
        from Utils.deployment import CustomRule
        return super().custom_routing_rules + [
            CustomRule(rule_id='monster_hunter_rise:9f9044efba36',
                dest="reframework/quests",
                extensions=[".json"],
                flatten=True,
                loose_only=True,
            ),
            CustomRule(rule_id='monster_hunter_rise:00d408b218c9',
                dest="reframework",
                folders=["quests"],
                flatten=True,
            ),
        ]
