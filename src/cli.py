#!/usr/bin/env python3
"""
cli.py
Command-line interface for Amethyst Mod Manager.

Usage:
    python cli.py list-games
    python cli.py list-profiles <game>
    python cli.py deploy <game> <profile_name>
    python cli.py launch <game> [--profile <name>] [--no-deploy]
    python cli.py restore <game>
    python cli.py clear-credentials

<game> can be the game's game_id (e.g. 'skyrim_se'), its full display name
(e.g. 'Skyrim Special Edition'), or a Steam app ID.  Matching is
case-insensitive.
"""

from __future__ import annotations

import argparse
import sys


def _setup_path():
    """Ensure src/ is on sys.path so Utils/Games/etc can be imported."""
    import app_bootstrap

    app_bootstrap.setup_environment()


def _find_game(games: dict, key: str):
    """Return a game instance matching key by name, game_id, or Steam app ID (case-insensitive)."""
    key_lower = key.lower()
    # Single pass; name match wins over game_id, which wins over Steam ID.
    by_game_id = None
    by_steam_id = None
    for name, game in games.items():
        if name.lower() == key_lower:
            return game
        if by_game_id is None and getattr(game, "game_id", "").lower() == key_lower:
            by_game_id = game
        if by_steam_id is None:
            sid = getattr(game, "steam_id", "")
            alt_ids = getattr(game, "alt_steam_ids", [])
            if key_lower == str(sid).lower() or any(
                    key_lower == str(a).lower() for a in alt_ids):
                by_steam_id = game
    return by_game_id if by_game_id is not None else by_steam_id


def _log(msg: str):
    print(msg, flush=True)


def cmd_list_games(games: dict):
    if not games:
        print("No games discovered.")
        return
    print(f"{'Game Name':<40} {'game_id':<30} {'Configured'}")
    print("-" * 80)
    for name, game in sorted(games.items()):
        configured = "yes" if game.is_configured() else "no"
        gid = getattr(game, "game_id", "")
        print(f"{name:<40} {gid:<30} {configured}")


def cmd_list_profiles(games: dict, key: str):
    game = _find_game(games, key)
    if game is None:
        print(f"Error: game '{key}' not found.", file=sys.stderr)
        sys.exit(1)
    profile_root = game.get_profile_root()
    profiles_dir = profile_root / "profiles"
    if not profiles_dir.is_dir():
        print(f"No profiles directory found at: {profiles_dir}")
        return
    profiles = sorted(p.name for p in profiles_dir.iterdir() if p.is_dir())
    if not profiles:
        print("No profiles found.")
    else:
        for p in profiles:
            print(p)


def cmd_deploy(games: dict, key: str, profile: str):
    game = _find_game(games, key)
    if game is None:
        print(f"Error: game '{key}' not found.", file=sys.stderr)
        sys.exit(1)
    if not game.is_configured():
        print(f"Error: game '{game.name}' is not configured (game path not set).", file=sys.stderr)
        sys.exit(1)

    from Utils.deploy_pipeline import run_deploy_pipeline

    profile_dir = game.get_profile_root() / "profiles" / profile
    if not profile_dir.is_dir():
        print(f"Error: profile '{profile}' does not exist at {profile_dir}", file=sys.stderr)
        sys.exit(1)

    success = run_deploy_pipeline(game, profile, log_fn=_log)
    if not success:
        sys.exit(1)

    _log(f"Deploy complete: {game.name} / {profile}")


def cmd_launch(games: dict, key: str, profile: "str | None" = None,
               deploy: bool = True,
               vanilla_command: "list[str] | None" = None):
    """Deploy, then replace this process with the game's launch command.

    Intended for a launcher wrapper, so the game remains a child of Steam,
    Heroic, Lutris, or Faugus and keeps that launcher's tracking/integration.
    The launcher appends its normal game command after ``--``. External loaders
    ignore it; profile-VFS handlers retarget it into the private game view.

    Only handlers that require a launch wrapper (``native_launch_required``)
    are supported: either a native external loader or a VFS wrapper around the
    command the launcher supplied. Everything else uses the normal GUI launch
    router.
    """
    import os

    game = _find_game(games, key)
    if game is None:
        print(f"Error: game '{key}' not found.", file=sys.stderr)
        sys.exit(1)
    if not game.is_configured():
        print(f"Error: game '{game.name}' is not configured (game path not set).",
              file=sys.stderr)
        sys.exit(1)
    # Default to the profile that was last DEPLOYED, not the one merely
    # selected in the GUI. That keeps a bare "launch <game>" launch option
    # correct across profile switches: the user switches and deploys in the
    # manager, and the configured launcher follows without its wrapper being
    # edited. Selecting a profile without deploying it is not a decision to
    # play it.
    if not profile:
        profile = (game.get_last_deployed_profile()
                   or game.get_last_active_profile() or "default")
    profile_dir = game.get_profile_root() / "profiles" / profile
    if not profile_dir.is_dir():
        print(f"Error: profile '{profile}' does not exist at {profile_dir}",
              file=sys.stderr)
        sys.exit(1)

    # Profile settings can change the launch mechanism (for example VFS),
    # so select and reload before checking native_launch_required.
    game.set_active_profile_dir(profile_dir)
    game.load_paths()
    if not hasattr(game, "get_launch_command"):
        print(f"Error: '{game.name}' cannot be launched from the CLI.",
              file=sys.stderr)
        sys.exit(1)
    requires_wrapper = bool(getattr(game, "native_launch_required", False))
    allows_passthrough = bool(
        vanilla_command
        and (
            getattr(game, "launch_passthrough_supported", False)
            or getattr(game, "steam_launch_passthrough_supported", False)
        )
    )
    if not requires_wrapper and not allows_passthrough:
        # Refusing beats launching the wrong thing: without a native command
        # this would need the full Proton/store routing, and a
        # launcher-owned process re-entering a store is unsafe.
        print(f"Error: '{game.name}' does not need an external launch wrapper. "
              "Launch it from the configured launcher as normal.",
              file=sys.stderr)
        sys.exit(1)

    if deploy:
        # Without this, toggling a mod in the GUI and then pressing Play in
        # a launcher would silently run the previous mod list.
        from Utils.deploy_pipeline import run_deploy_pipeline
        _log(f"Deploying {game.name} / {profile} ...")
        if not run_deploy_pipeline(game, profile, log_fn=_log):
            print("Error: deploy failed - refusing to launch.", file=sys.stderr)
            sys.exit(1)

    cmd = None
    try:
        vfs_builder = getattr(game, "get_vfs_passthrough_command", None)
        if not callable(vfs_builder):
            vfs_builder = getattr(game, "get_vfs_steam_command", None)
        if getattr(game, "vfs_launch_enabled", False) and callable(vfs_builder):
            if not vanilla_command:
                raise RuntimeError(
                    "The launcher's game command was not supplied after '--'. "
                    "Copy the complete wrapper settings from Amethyst.")
            cmd = vfs_builder(vanilla_command)
        elif allows_passthrough:
            cmd = list(vanilla_command or [])
        else:
            cmd = game.get_launch_command()
    except Exception as exc:
        print(f"Error: could not build the launch command: {exc}",
              file=sys.stderr)
        sys.exit(1)
    if not cmd:
        reason = ""
        try:
            reason = game.native_launch_blocked_reason() or ""
        except Exception:
            pass
        print(f"Error: cannot launch {game.name} - "
              f"{reason or 'the mod loader is not ready.'}", file=sys.stderr)
        sys.exit(1)

    _log("Launching: " + " ".join(cmd))
    try:
        # exec, don't spawn: launchers track the process they started, so
        # replacing it keeps their Stop button attached to the real game.
        os.execvp(cmd[0], cmd)
    except OSError as exc:
        print(f"Error: could not run {cmd[0]}: {exc}", file=sys.stderr)
        sys.exit(1)


def cmd_clear_credentials():
    from Nexus.nexus_api import clear_api_key
    from Nexus.nexus_oauth import clear_oauth_tokens
    clear_api_key()
    clear_oauth_tokens()
    print("Nexus credentials cleared.")


def cmd_restore(games: dict, key: str):
    game = _find_game(games, key)
    if game is None:
        print(f"Error: game '{key}' not found.", file=sys.stderr)
        sys.exit(1)
    if not game.is_configured():
        print(f"Error: game '{game.name}' is not configured (game path not set).", file=sys.stderr)
        sys.exit(1)

    from Utils.deploy import restore_root_folder_for_game

    game_root = game.get_game_path()
    profile_root = game.get_profile_root()

    last_deployed = game.get_last_deployed_profile()
    if last_deployed:
        game.set_active_profile_dir(profile_root / "profiles" / last_deployed)
        # Reload so the last-deployed profile's path overrides drive the restore.
        game.load_paths()
        game_root = game.get_game_path()

    if hasattr(game, "restore"):
        game.restore(log_fn=_log)
    else:
        print(f"Game '{game.name}' does not support restore.")

    root_folder_dir = game.get_effective_root_folder_path()
    if root_folder_dir.is_dir() and game_root:
        restore_root_folder_for_game(
            game, root_folder_dir=root_folder_dir,
            game_root=game_root, log_fn=_log,
        )

    _log(f"Restore complete: {game.name}")


def main():
    _setup_path()

    parser = argparse.ArgumentParser(
        prog="amethyst",
        description="Amethyst Mod Manager - CLI",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("list-games", help="List all discovered games and whether they are configured")

    lp = subparsers.add_parser("list-profiles", help="List profiles for a game")
    lp.add_argument("game", help="game_id or display name (case-insensitive)")

    dp = subparsers.add_parser("deploy", help="Build filemap and deploy mods for a profile")
    dp.add_argument("game", help="game_id or display name (case-insensitive)")
    dp.add_argument("profile", help="Profile name")

    gp = subparsers.add_parser(
        "launch", help="Deploy, then launch the game through its mod loader")
    gp.add_argument("game", help="game_id or display name (case-insensitive)")
    gp.add_argument("--profile", default=None,
                    help="Profile to deploy and launch (default: last active)")
    gp.add_argument("--no-deploy", action="store_true",
                    help="Launch the existing mod list without deploying first")

    rp = subparsers.add_parser("restore", help="Restore the game directory (undo last deploy)")
    rp.add_argument("game", help="game_id or display name (case-insensitive)")

    subparsers.add_parser("clear-credentials", help="Remove stored Nexus Mods API key and OAuth tokens")

    # Launcher wrappers append the vanilla launch command after ``--`` (Steam
    # gets it from %command%; Heroic/Lutris/Faugus append it implicitly).
    # Retain that command for VFS handlers; external loaders simply ignore it.
    argv = sys.argv[1:]
    vanilla_command: list[str] = []
    if "--" in argv:
        separator = argv.index("--")
        vanilla_command = argv[separator + 1:]
        argv = argv[:separator]
    args = parser.parse_args(argv)

    if args.command == "clear-credentials":
        cmd_clear_credentials()
        return

    from Utils.game_loader import discover_games
    games = discover_games()

    if args.command == "list-games":
        cmd_list_games(games)
    elif args.command == "list-profiles":
        cmd_list_profiles(games, args.game)
    elif args.command == "deploy":
        cmd_deploy(games, args.game, args.profile)
    elif args.command == "launch":
        cmd_launch(games, args.game, args.profile, deploy=not args.no_deploy,
                   vanilla_command=vanilla_command)
    elif args.command == "restore":
        cmd_restore(games, args.game)


if __name__ == "__main__":
    main()
