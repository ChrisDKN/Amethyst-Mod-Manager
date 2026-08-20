"""Compatibility import for the former Steam-only handoff overlay."""

from types import SimpleNamespace

from gui_qt.launch_handoff_overlay import LaunchHandoffOverlay


class SteamLaunchCommandOverlay(LaunchHandoffOverlay):
    """Adapt the old ``(host, game, launch_string)`` constructor."""

    def __init__(self, host, game_name: str, launch_string: str, on_done=None):
        handoff = SimpleNamespace(
            launcher_id="steam",
            launcher_name="Steam",
            instructions=(
                "Open Properties → General and paste this into Launch Options."
            ),
            fields=(SimpleNamespace(
                label="Launch Options", value=launch_string),),
            note=(
                "Set this once. It deploys and launches whichever profile was "
                "last deployed in Amethyst."
            ),
        )
        super().__init__(host, game_name, handoff, on_done=on_done)
