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
import hashlib
import re
import shlex


_LAUNCHER_FLATPAK_IDS = {
    "steam": "com.valvesoftware.Steam",
    "heroic": "com.heroicgameslauncher.hgl",
    "lutris": "net.lutris.Lutris",
    "faugus": "io.github.Faugus.faugus-launcher",
}

# Runtime values a launcher commonly adds immediately before its custom
# wrapper. flatpak-spawn starts with the host desktop environment, so these
# must cross the sandbox boundary explicitly. Avoid forwarding PATH/XDG/LD_*
# because those describe the launcher's Flatpak runtime, not the host.
_FLATPAK_HANDOFF_ENV = (
    "WINEPREFIX",
    "PROTONPATH",
    "GAMEID",
    "WINEDLLOVERRIDES",
    "WINEDEBUG",
    "SteamAppId",
    "SteamGameId",
    "SteamOverlayGameId",
    "STEAM_COMPAT_APP_ID",
    "STEAM_COMPAT_DATA_PATH",
    "STEAM_COMPAT_INSTALL_PATH",
    "STEAM_COMPAT_CLIENT_INSTALL_PATH",
    "STEAM_COMPAT_TOOL_PATHS",
    "STEAM_COMPAT_MOUNTS",
    "SteamEnv",
    "SteamPath",
    "PROTON_LOG",
    "PROTON_USE_WINED3D",
    "PROTON_NO_ESYNC",
    "PROTON_NO_FSYNC",
    "PROTON_ENABLE_NVAPI",
    "PROTON_ENABLE_WAYLAND",
    "DXVK_CONFIG_FILE",
    "VKD3D_CONFIG",
    "DRI_PRIME",
    "MANGOHUD",
    "MANGOHUD_CONFIG",
    "LSFGVK_ENV",
    "LSFGVK_CONFIG",
    "LSFGVK_PROFILE",
    "LSFGVK_DLL_PATH",
    "LSFGVK_NO_FP16",
    "LSFGVK_LOG_LEVEL",
    "LSFGVK_LOG_FILE",
    "LSFGVK_MULTIPLIER",
    "LSFGVK_FLOW_SCALE",
    "LSFGVK_PERFORMANCE_MODE",
    "LSFGVK_PACING_MODE",
    "LSFGVK_OVERRIDE_PRESENT_MODE",
    "LSFGVK_PRESERVE_SWAPCHAIN_IMAGE_COUNT",
    "LSFG_LEGACY",
    "LSFG_DLL_PATH",
    "LSFG_MULTIPLIER",
    "LSFG_FLOW_SCALE",
    "LSFG_PERFORMANCE_MODE",
    "LSFG_HDR_MODE",
    "LSFG_EXPERIMENTAL_PRESENT_MODE",
)


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
    from Utils.launchers.heroic import (
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


def _wabbajack_launcher_source_path(game) -> Path | None:
    """Base install copied by an active Wabbajack Stock Game profile."""
    profile = getattr(game, "_active_profile_dir", None)
    if profile is None:
        return None
    try:
        from Utils.profiles.state import read_profile_settings
        settings = read_profile_settings(Path(profile))
    except Exception:
        return None
    if not (settings.get("wabbajack_install_id")
            and settings.get("game_path")):
        return None
    getter = getattr(game, "get_global_game_path", None)
    if not callable(getter):
        return None
    try:
        return getter()
    except Exception:
        return None


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
            from Utils.flatpak.sandbox import (
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
            from Utils.launchers.lutris import find_lutris_launch_info
            info = find_lutris_launch_info([lutris])
            return "lutris", bool(
                info[1] if info else _flatpak_data_exists("net.lutris.Lutris")
            )
        except Exception:
            return "lutris", _flatpak_data_exists("net.lutris.Lutris")

    faugus = _saved_id(game, "faugus_gameid")
    if faugus:
        try:
            from Utils.launchers.faugus import find_faugus_launch_info
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
    from Utils.executables.launch import (
        faugus_gameids_for_launch,
        game_is_steam_install,
        heroic_app_names_for_launch,
        lutris_slugs_for_launch,
        path_is_steam_install,
    )
    steam_path = None
    if game_is_steam_install(game):
        steam_path = game.get_game_path()
    else:
        source_path = _wabbajack_launcher_source_path(game)
        if path_is_steam_install(source_path):
            steam_path = source_path
    if steam_path is not None:
        try:
            from Utils.flatpak.sandbox import (
                STEAM_FLATPAK_ID,
                sandbox_app_for_game,
            )
            app = sandbox_app_for_game(game, steam_path)
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
        from Utils.launchers.lutris import find_lutris_launch_info
        info = find_lutris_launch_info(lutris_slugs_for_launch(game))
        if info:
            return "lutris", bool(info[1])
    except Exception:
        pass

    try:
        from Utils.launchers.faugus import find_faugus_launch_info
        info = find_faugus_launch_info(faugus_gameids_for_launch(game))
        if info:
            return "faugus", bool(info[1])
    except Exception:
        pass
    return None


def flatpak_launcher_app_for_handoff(game) -> str | None:
    """Flatpak app id used by this game's required launcher handoff."""
    detected = _detected_launcher(game)
    if detected is None:
        return None
    launcher, is_flatpak = detected
    if not is_flatpak:
        return None
    return _LAUNCHER_FLATPAK_IDS.get(launcher)


def _launch_argv(game, profile: str | None) -> list[str]:
    from Utils.config_paths import cli_invocation

    argv = [*cli_invocation(), "launch", game.game_id]
    if profile:
        argv += ["--profile", profile]
    return argv


def launch_handoff_script_path(game, profile: str | None = None) -> Path:
    """Stable, launcher-visible path for a game's generated handoff script.

    Do not place this below the active profile: profile-specific staging roots
    can change when the user switches lists, while Steam/Heroic/Lutris/Faugus
    keep one saved command. The shared Amethyst launchers directory remains
    stable and can be granted to a launcher Flatpak without exposing all of
    the user's home directory.
    """
    from Utils.config_paths import get_default_staging_root

    raw_id = str(getattr(game, "game_id", "game") or "game")
    slug = re.sub(r"[^A-Za-z0-9._-]+", "_", raw_id).strip("._-") or "game"
    if slug != raw_id or len(slug) > 80:
        digest = hashlib.sha256(raw_id.encode("utf-8")).hexdigest()[:10]
        slug = f"{slug[:64] or 'game'}-{digest}"
    if profile:
        digest = hashlib.sha256(profile.encode("utf-8")).hexdigest()[:10]
        slug = f"{slug}-profile-{digest}"
    return get_default_staging_root() / "launchers" / f"{slug}.sh"


def _handoff_native_target(game) -> tuple[bool, Path | None]:
    """Return whether the generated handoff ultimately starts a native path."""
    command = None
    target = None
    if getattr(game, "vfs_launch_enabled", False):
        try:
            target = game.get_vfs_launch_exe()
        except Exception:
            target = None
        if target is not None:
            target = Path(target)
            return target.suffix.lower() not in (".exe", ".bat"), target
    else:
        try:
            command = game.get_launch_command()
        except Exception:
            command = None
    if command:
        # A native loader command may itself start Proton (for example me3),
        # but it still needs the same environment manager Play would supply.
        if len(command) == 1:
            candidate = Path(command[0])
            target = candidate if candidate.is_file() else None
        return True, target
    return False, None


def _handoff_launch_settings(game) -> tuple[dict[str, str], list[str]]:
    """Return Amethyst-owned environment and suffix arguments for a handoff."""
    from Utils.executables.launch import (
        apply_lsfg_launch_setting,
        apply_wayland_launch_setting,
        load_launch_with_wayland,
    )

    env: dict[str, str] = {}
    marker = "%command%"
    command = [marker]
    wayland = load_launch_with_wayland(game)
    native, target = (
        _handoff_native_target(game)
        if wayland else (False, None)
    )
    command = apply_wayland_launch_setting(
        game, env, command, native=native, exe_path=target, enabled=wayland)
    apply_lsfg_launch_setting(game, env)
    return env, command[command.index(marker) + 1:]


def compose_steam_handoff_command(game, handoff_argv: list[str]) -> str:
    """Compose user launch options around the short Steam handoff."""
    from Utils.executables.launch import (
        game_exe_key,
        load_launch_options,
        parse_launch_options,
    )

    settings_key = game_exe_key(game)
    launch_options = load_launch_options(game, settings_key)
    env, command = parse_launch_options(
        launch_options, [*map(str, handoff_argv), "%command%"])

    assignments = " ".join(
        f"{name}={shlex.quote(str(value))}" for name, value in env.items()
    )
    rendered = shlex.join(command)
    return f"{assignments} {rendered}" if assignments else rendered


def _flatpak_handoff_argv(argv: list[str], marker: str) -> list[str]:
    """Shell wrapper that preserves a launcher's runner argv and environment.

    The launcher appends its original command after this wrapper. The shell
    first makes that command part of Amethyst's CLI argv, prepends an
    ``--env`` option for each runtime value which is actually set, then
    escapes to the host. Positional arguments keep paths and values with
    whitespace intact without evaluating launcher-provided text as shell.
    """
    script = f"set -- {shlex.join(argv)} -- \"$@\"; "
    for name in _FLATPAK_HANDOFF_ENV:
        script += (
            f'if [ "${{{name}+x}}" = x ]; then '
            f'set -- "--env={name}=${{{name}}}" "$@"; fi; '
        )
    script += 'exec /usr/bin/flatpak-spawn --host "$@"'
    return ["/bin/sh", "-c", script, marker]


def _flatpak_vfs_handoff_argv(argv: list[str], marker: str) -> list[str]:
    """Coordinate on the host, then run the game inside its launcher Flatpak.

    Heroic, Lutris, Faugus and Flatpak Steam append commands containing
    sandbox-private paths such as ``/app/bin/gamemoderun``.  Sending that argv
    through ``flatpak-spawn --host`` makes it immediately invalid.  Instead,
    Amethyst deploys on the host and prints one shell-quoted command retargeted
    to the private VFS view.  This launcher-side shell evaluates only that
    Amethyst-generated string, keeping the original runner and its environment
    in the sandbox which owns them.
    """
    bridge_argv = [*argv, "--sandbox-bridge"]
    coordinator = shlex.join([
        "/usr/bin/flatpak-spawn", "--host", *bridge_argv,
    ])
    script = (
        f'payload="$({coordinator} -- "$@")" || exit $?; '
        'encoded="${payload##*$\'\\n\'}"; '
        'if [[ "$encoded" != AMETHYST_VFS_BRIDGE:* ]]; then '
        'echo "Amethyst did not return a valid launcher command." >&2; '
        'exit 1; fi; '
        'bridge="$(printf %s "${encoded#AMETHYST_VFS_BRIDGE:}" | '
        '/usr/bin/base64 --decode)" || exit $?; '
        'eval "$bridge"'
    )
    return ["/usr/bin/bash", "-c", script, marker]


def _write_launch_handoff_script(
    game,
    profile: str | None,
    launcher: str,
    is_flatpak: bool,
) -> Path:
    """Atomically refresh the short script saved in an external launcher."""
    from Utils.atomic_write import write_atomic_text

    argv = _launch_argv(game, profile)
    if is_flatpak:
        wrapper_factory = (
            _flatpak_vfs_handoff_argv
            if getattr(game, "vfs_launch_enabled", False)
            else _flatpak_handoff_argv
        )
        wrapper = wrapper_factory(argv, f"amethyst-{launcher}")
    else:
        wrapper = [*argv, "--"]

    env, suffix = _handoff_launch_settings(game)
    exports = "".join(
        f"export {name}={shlex.quote(str(value))}\n"
        for name, value in env.items()
    )
    append_suffix = (
        f"set -- \"$@\" {shlex.join(suffix)}\n" if suffix else ""
    )

    # Each launcher is configured with a small, explicit ``--`` marker. Strip
    # that marker, then pass the launcher-owned command through only as argv.
    script = (
        "#!/usr/bin/bash\n"
        "# Generated by Amethyst Mod Manager; refreshed after every deploy.\n"
        'if [ "${1-}" = "--" ]; then shift; fi\n'
        f"{exports}"
        f"{append_suffix}"
        f"exec {shlex.join(wrapper)} \"$@\"\n"
    )
    path = launch_handoff_script_path(game, profile)
    write_atomic_text(path, script)
    path.chmod(0o755)
    return path


def refresh_launch_handoff_script(
    game,
    profile: str | None = None,
    *,
    log_fn=None,
) -> Path | None:
    """Refresh a configured game's stable short launcher script, if needed."""
    if not getattr(game, "native_launch_required", False):
        return None
    detected = _detected_launcher(game)
    if detected is None:
        return None
    launcher, is_flatpak = detected
    path = _write_launch_handoff_script(
        game, profile, launcher, is_flatpak)
    if log_fn is not None:
        log_fn(f"Launcher handoff: refreshed {path}.")
    return path


def build_launch_handoff(game, profile: str | None = None
                         ) -> LaunchHandoff | None:
    """Build the appropriate launcher settings for *game*'s active profile."""
    if not getattr(game, "native_launch_required", False):
        return None
    detected = _detected_launcher(game)
    if detected is None:
        return None

    launcher, is_flatpak = detected
    script_path = _write_launch_handoff_script(
        game, profile, launcher, is_flatpak)
    short_argv = [str(script_path), "--"]
    note = (
        "Set this once. It launches whichever profile is currently deployed "
        "in Amethyst, so switching profiles needs no launcher edit. If no "
        "profile is deployed, the wrapper becomes transparent and the game "
        "launches normally without mods. Amethyst refreshes the generated "
        "script after each successful deploy."
    )

    if launcher == "steam":
        command = compose_steam_handoff_command(game, short_argv)
        return LaunchHandoff(
            launcher_id="steam",
            launcher_name="Steam",
            instructions=(
                "Open Properties → General and paste this into Launch Options."
            ),
            fields=(LaunchHandoffField("Launch Options", command),),
            note=note,
        )

    wrapper = short_argv

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
        fields = [LaunchHandoffField("Launch Arguments", command)]
        instructions = (
            "Edit the game and open Launch Settings. Add the Launch Arguments "
            "value below as one row in the Launch Arguments list. Do not put "
            "it in Game Arguments, Pre-launch, or Post-launch."
        )
        if is_flatpak:
            instructions = (
                "Amethyst grants the required Flatpak permission during "
                "deploy; restart Faugus if Amethyst asks you to. "
                + instructions
            )
        return LaunchHandoff(
            launcher_id="faugus",
            launcher_name="Faugus",
            instructions=instructions,
            fields=tuple(fields),
            note=note,
        )
    return None
