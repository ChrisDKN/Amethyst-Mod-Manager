"""Launcher-aware post-deploy handoff commands.

Games backed by a native loader or the profile VFS need Amethyst to remain in
the launch chain.  Each launcher exposes that chain differently: Steam has a
``%command%`` placeholder, Heroic has structured custom wrappers, Lutris has a
command-prefix field, and Faugus prepends its launch-arguments string.

This module keeps launcher detection and command construction toolkit-neutral
so the GUI only has to render the resulting fields.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import shlex


@dataclass(frozen=True)
class LaunchHandoffField:
    """One launcher setting the user must fill in."""

    label: str
    value: str


@dataclass(frozen=True)
class LaunchHandoff:
    """A launcher-specific command plus concise placement instructions."""

    launcher_id: str
    launcher_name: str
    instructions: str
    fields: tuple[LaunchHandoffField, ...]
    note: str


def _saved_id(game, key: str) -> str:
    getter = getattr(game, "get_saved_launcher_id", None)
    if not callable(getter):
        return ""
    try:
        return str(getter(key) or "").strip()
    except Exception:
        return ""


def _flatpak_data_exists(app_id: str) -> bool:
    return (Path.home() / ".var" / "app" / app_id).is_dir()


def _heroic_launch_is_flatpak(app_names: list[str]) -> bool | None:
    """Flatpak identity for Heroic's matched entry, or None when unmatched.

    Heroic's long-standing public lookup returns only ``(store, app_name)``.
    Reuse that result and its config-root readers here so launcher handoff
    detection does not alter the API used by the existing launch router.
    """
    from Utils.heroic_finder import (
        _find_heroic_config_roots,
        _load_epic_installed,
        _load_gog_installed,
        _load_sideload_installed,
        find_heroic_launch_info,
    )

    info = find_heroic_launch_info(app_names)
    if not info:
        return None
    store, matched = info
    wanted = matched.casefold()
    for root in _find_heroic_config_roots():
        found = False
        if store == "legendary":
            found = matched in _load_epic_installed(root)
        elif store == "gog":
            found = any(
                str(entry.get("appName") or entry.get("app_name") or "")
                .casefold() == wanted
                for entry in _load_gog_installed(root)
                if isinstance(entry, dict)
            )
        elif store == "sideload":
            found = any(
                str(entry.get("app_name") or entry.get("appName") or "")
                .casefold() == wanted
                for entry in _load_sideload_installed(root)
                if isinstance(entry, dict)
            )
        if found:
            return "com.heroicgameslauncher.hgl" in root.parts
    return _flatpak_data_exists("com.heroicgameslauncher.hgl")


def _detected_launcher(game) -> tuple[str, bool] | None:
    """Return ``(launcher_id, is_flatpak)`` for the active profile.

    Configure Game stores mutually-exclusive launcher IDs with the profile.
    Those IDs win because two launchers can deliberately point at the same
    installation.  Live install detection is only the fallback for older
    configurations that predate those IDs.
    """
    shortcut = _saved_id(game, "shortcut_appid")
    if shortcut:
        try:
            from Utils.flatpak_sandbox import (
                STEAM_FLATPAK_ID,
                sandbox_app_for_game,
            )
            app = sandbox_app_for_game(game, game.get_game_path())
            return "steam", app == STEAM_FLATPAK_ID
        except Exception:
            return "steam", False

    heroic = _saved_id(game, "heroic_app_name")
    if heroic:
        try:
            info = _heroic_launch_is_flatpak([heroic])
            return "heroic", bool(
                info if info is not None else _flatpak_data_exists(
                    "com.heroicgameslauncher.hgl")
            )
        except Exception:
            return "heroic", _flatpak_data_exists(
                "com.heroicgameslauncher.hgl")

    lutris = _saved_id(game, "lutris_slug")
    if lutris:
        try:
            from Utils.lutris_finder import find_lutris_launch_info
            info = find_lutris_launch_info([lutris])
            return "lutris", bool(
                info[1] if info else _flatpak_data_exists("net.lutris.Lutris")
            )
        except Exception:
            return "lutris", _flatpak_data_exists("net.lutris.Lutris")

    faugus = _saved_id(game, "faugus_gameid")
    if faugus:
        try:
            from Utils.faugus_finder import find_faugus_launch_info
            info = find_faugus_launch_info([faugus])
            return "faugus", bool(
                info[1] if info else _flatpak_data_exists(
                    "io.github.Faugus.faugus-launcher")
            )
        except Exception:
            return "faugus", _flatpak_data_exists(
                "io.github.Faugus.faugus-launcher")

    # Older configurations have no pinned source. Match the launch router's
    # automatic order so this notice agrees with the Play button.
    from Utils.exe_launch import (
        faugus_gameids_for_launch,
        game_is_steam_install,
        heroic_app_names_for_launch,
        lutris_slugs_for_launch,
    )
    if game_is_steam_install(game):
        try:
            from Utils.flatpak_sandbox import (
                STEAM_FLATPAK_ID,
                sandbox_app_for_game,
            )
            app = sandbox_app_for_game(game, game.get_game_path())
            return "steam", app == STEAM_FLATPAK_ID
        except Exception:
            return "steam", False

    try:
        info = _heroic_launch_is_flatpak(heroic_app_names_for_launch(game))
        if info is not None:
            return "heroic", info
    except Exception:
        pass

    try:
        from Utils.lutris_finder import find_lutris_launch_info
        info = find_lutris_launch_info(lutris_slugs_for_launch(game))
        if info:
            return "lutris", bool(info[1])
    except Exception:
        pass

    try:
        from Utils.faugus_finder import find_faugus_launch_info
        info = find_faugus_launch_info(faugus_gameids_for_launch(game))
        if info:
            return "faugus", bool(info[1])
    except Exception:
        pass
    return None


def _launch_argv(game, profile: str | None) -> list[str]:
    from Utils.config_paths import cli_invocation

    argv = [*cli_invocation(), "launch", game.game_id]
    if profile:
        argv += ["--profile", profile]
    return argv


def _launcher_wrapper_argv(argv: list[str], is_flatpak: bool) -> list[str]:
    """Make a host CLI invocation callable from a launcher Flatpak.

    Heroic/Faugus/Lutris store their UMU tools below ``~/.var/app`` at paths
    also visible on the host.  Escaping before invoking Amethyst lets source,
    AppImage, native-package and Amethyst-Flatpak installs share one command.
    """
    if is_flatpak:
        return ["/usr/bin/flatpak-spawn", "--host", *argv]
    return argv


def build_launch_handoff(game, profile: str | None = None
                         ) -> LaunchHandoff | None:
    """Build the appropriate launcher settings for *game*'s active profile."""
    if not getattr(game, "native_launch_required", False):
        return None
    detected = _detected_launcher(game)
    if detected is None:
        return None

    launcher, is_flatpak = detected
    argv = _launch_argv(game, profile)
    note = (
        "Set this once. It deploys and launches whichever profile was last "
        "deployed in Amethyst, so switching profiles needs no launcher edit."
    )

    if launcher == "steam":
        command = shlex.join(argv) + " -- %command%"
        return LaunchHandoff(
            launcher_id="steam",
            launcher_name="Steam",
            instructions=(
                "Open Properties → General and paste this into Launch Options."
            ),
            fields=(LaunchHandoffField("Launch Options", command),),
            note=note,
        )

    wrapper = _launcher_wrapper_argv(argv, is_flatpak)
    # The launcher's original runner command is appended after this separator.
    wrapper.append("--")

    if launcher == "heroic":
        return LaunchHandoff(
            launcher_id="heroic",
            launcher_name="Heroic",
            instructions=(
                "Open the game's Settings → Advanced, add a Custom Wrapper, "
                "then fill in these two values."
            ),
            fields=(
                LaunchHandoffField("Wrapper executable", wrapper[0]),
                LaunchHandoffField("Wrapper arguments", shlex.join(wrapper[1:])),
            ),
            note=note,
        )

    command = shlex.join(wrapper)
    if launcher == "lutris":
        return LaunchHandoff(
            launcher_id="lutris",
            launcher_name="Lutris",
            instructions=(
                "Open Configure → System options, enable Advanced options, "
                "and paste this into Command prefix."
            ),
            fields=(LaunchHandoffField("Command prefix", command),),
            note=note,
        )

    if launcher == "faugus":
        return LaunchHandoff(
            launcher_id="faugus",
            launcher_name="Faugus",
            instructions=(
                "Edit the game, open Launch Settings, and add this to Launch "
                "Arguments. Keep any existing environment variables."
            ),
            fields=(LaunchHandoffField("Launch Arguments", command),),
            note=note,
        )
    return None
