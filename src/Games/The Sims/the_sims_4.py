from importlib import import_module

from Utils.deployment import CustomRule

SimsBase = import_module("Games.The Sims.sims_base").SimsBase

_TRAY_EXTENSIONS = [
    ".householdbinary", ".trayitem", ".sgi", ".hhi", ".blueprint", ".room", ".rmi", ".bpi",
]


class TheSims4(SimsBase):
    @property
    def vortex_mod_types(self) -> dict:
        return {
            "sims4mixed": {
                "dest": str(self.user_subpath),
                "to_prefix": True,
            },
        }

    @property
    def name(self) -> str:
        return "The Sims 4"

    @property
    def game_id(self) -> str:
        return "the_sims_4"

    @property
    def exe_name(self) -> str:
        return "Game/Bin/TS4_x64.exe"

    @property
    def exe_name_alts(self) -> list[str]:
        return ["Game/Bin/TS4_Launcher_x64.exe"]

    @property
    def steam_id(self) -> str:
        return "1222670"

    @property
    def nexus_game_domain(self) -> str:
        return "thesims4"

    @property
    def mod_install_extensions(self) -> set[str]:
        return {".package", ".ts4script", *_TRAY_EXTENSIONS}

    @property
    def custom_routing_rules(self) -> list[CustomRule]:
        return [
            CustomRule(
                rule_id="the_sims_4:764037fc0f9a",
                dest=str(self.user_subpath / "Tray"),
                extensions=_TRAY_EXTENSIONS,
                flatten=True,
                to_prefix=True,
            ),
        ]
