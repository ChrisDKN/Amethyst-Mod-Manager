"""Cult of the Lamb game handler."""

from Games.base_game import WizardTool
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

    @property
    def wizard_tools(self) -> list[WizardTool]:
        # The pack keeps its BepInExPack_CultOfTheLamb/ wrapper folder (same as
        # a Thunderstore install); it is stripped when the filemap is built.
        return self._base_wizard_tools() + [WizardTool(
            id="install_bepinex",
            label="Install BepInEx",
            description=(
                "Download BepInExPack for Cult of the Lamb from Thunderstore "
                "and add it as a mod."
            ),
            dialog_class_path="wizards.script_extender.ScriptExtenderWizard",
            extra={
                "direct_download_url": (
                    "https://gcdn.thunderstore.io/live/repository/packages/"
                    "BepInEx-BepInExPack_CultOfTheLamb-5.4.2101.zip"
                ),
                "download_url": (
                    "https://thunderstore.io/c/cult-of-the-lamb/p/BepInEx/"
                    "BepInExPack_CultOfTheLamb/"
                ),
                "archive_keywords": ["bepinexpack_cultofthelamb"],
                "strip_top_dir": False,
            },
        )]
