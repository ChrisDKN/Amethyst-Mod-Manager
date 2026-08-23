"""Compatibility import for the former Steam-only handoff overlay."""

from types import SimpleNamespace

from PySide6.QtCore import QCoreApplication

from gui_qt.launch_handoff_overlay import LaunchHandoffOverlay


class SteamLaunchCommandOverlay(LaunchHandoffOverlay):
    """Adapt the old ``(host, game, launch_string)`` constructor."""

    def __init__(self, host, game_name: str, launch_string: str, on_done=None):
        # The shared overlay renders these verbatim, so translate here. Context
        # is pinned explicitly because self.tr() is not usable before super(),
        # and the literals are spelled out at each call because lupdate does not
        # extract strings passed through a local alias.
        handoff = SimpleNamespace(
            launcher_id="steam",
            launcher_name="Steam",
            instructions=QCoreApplication.translate(
                "SteamLaunchCommandOverlay",
                "Open Properties → General and paste this into Launch Options."),
            fields=(SimpleNamespace(
                label=QCoreApplication.translate(
                    "SteamLaunchCommandOverlay", "Launch Options"),
                value=launch_string),),
            note=QCoreApplication.translate(
                "SteamLaunchCommandOverlay",
                "Set this once. It deploys and launches whichever profile was "
                "last deployed in Amethyst."),
        )
        super().__init__(host, game_name, handoff, on_done=on_done)
