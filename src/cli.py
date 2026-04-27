#!/usr/bin/env python3
"""
cli.py
Command-line interface for Amethyst Mod Manager.

Usage:
    python cli.py --deploy <game_id_or_name> <profile_name>
    python cli.py --restore <game_id_or_name>
    python cli.py --list-games
    python cli.py --list-profiles <game_id_or_name>
    python cli.py --clear-credentials

game_id_or_name can be either the game's game_id (e.g. 'skyrim_se') or its
full display name (e.g. 'Skyrim Special Edition').  Matching is case-insensitive.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path


def _setup_path():
    """Ensure src/ is on sys.path so Utils/Games/etc can be imported."""
    src = Path(__file__).resolve().parent
    if str(src) not in sys.path:
        sys.path.insert(0, str(src))

    # Set MOD_MANAGER_GAMES so game_loader can find the Games/ directory.
    if not os.environ.get("MOD_MANAGER_GAMES"):
        games_dir = src / "Games"
        if games_dir.is_dir():
            os.environ["MOD_MANAGER_GAMES"] = str(games_dir)


def _find_game(games: dict, key: str):
    """Return a game instance matching key by name, game_id, or Steam app ID (case-insensitive)."""
    key_lower = key.lower()
    # Exact name match first
    for name, game in games.items():
        if name.lower() == key_lower:
            return game
    # game_id match
    for game in games.values():
        if getattr(game, "game_id", "").lower() == key_lower:
            return game
    # Steam ID match (primary + alts)
    for game in games.values():
        sid = getattr(game, "steam_id", "")
        alt_ids = getattr(game, "alt_steam_ids", [])
        if key_lower == str(sid).lower() or key_lower in [str(a).lower() for a in alt_ids]:
            return game
    return None


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

    from Utils.deploy import deploy_root_folder, restore_root_folder, LinkMode, load_per_mod_strip_prefixes, deploy_root_flagged_mods
    from Utils.filemap import build_filemap
    from Utils.profile_backup import create_backup
    from Utils.ui_config import load_normalize_folder_case as _load_norm_case

    profile_root = game.get_profile_root()
    profile_dir = profile_root / "profiles" / profile
    if not profile_dir.is_dir():
        print(f"Error: profile '{profile}' does not exist at {profile_dir}", file=sys.stderr)
        sys.exit(1)

    game_root = game.get_game_path()

    # Restore using the last-deployed profile first
    last_deployed = game.get_last_deployed_profile()
    if last_deployed:
        game.set_active_profile_dir(profile_root / "profiles" / last_deployed)
    if getattr(game, "restore_before_deploy", True) and hasattr(game, "restore"):
        try:
            game.restore(log_fn=_log)
        except RuntimeError:
            pass
    # Restore Root_Folder for the last-deployed profile
    last_root_folder_dir = game.get_effective_root_folder_path()
    if last_root_folder_dir.is_dir() and game_root:
        restore_root_folder(last_root_folder_dir, game_root, log_fn=_log)

    # Switch to target profile
    game.set_active_profile_dir(profile_dir)

    # Rebuild filemap
    staging = game.get_effective_mod_staging_path()
    modlist_path = profile_dir / "modlist.txt"
    filemap_out = staging.parent / "filemap.txt"
    if modlist_path.is_file():
        try:
            from Utils.profile_state import read_excluded_mod_files as _read_exc
            from Nexus.nexus_meta import collect_root_flagged_mods as _collect_rf
            _exc_raw = _read_exc(profile_dir, None)
            _exc = {k: set(v) for k, v in _exc_raw.items()} if _exc_raw else None
            _rf_mods = _collect_rf(modlist_path, staging, log_fn=_log)
            build_filemap(
                modlist_path, staging, filemap_out,
                strip_prefixes=game.mod_folder_strip_prefixes or None,
                per_mod_strip_prefixes=load_per_mod_strip_prefixes(profile_dir),
                allowed_extensions=game.mod_install_extensions or None,
                root_deploy_folders=game.mod_root_deploy_folders or None,
                excluded_mod_files=_exc,
                conflict_ignore_filenames=getattr(game, "conflict_ignore_filenames", None) or None,
                normalize_folder_case=getattr(game, "normalize_folder_case", True) and _load_norm_case(),
                filemap_casing=getattr(game, "filemap_casing", "upper"),
                conflict_key_fn=getattr(game, "filemap_conflict_key_fn", None),
                exclude_dirs=getattr(game, "filemap_exclude_dirs", None) or None,
                root_folder_mods=_rf_mods or None,
            )
        except Exception as fm_err:
            _log(f"Filemap rebuild warning: {fm_err}")

    # Backup before deploy
    try:
        create_backup(profile_dir, _log)
    except Exception as backup_err:
        _log(f"Backup skipped: {backup_err}")

    # Deploy mods
    deploy_mode = game.get_deploy_mode() if hasattr(game, "get_deploy_mode") else LinkMode.HARDLINK
    game.deploy(log_fn=_log, profile=profile, mode=deploy_mode)

    # Apply Wine DLL overrides (user-added + handler-defined)
    from Utils.wine_dll_config import deploy_game_wine_dll_overrides
    _pfx = game.get_prefix_path()
    if _pfx and _pfx.is_dir():
        deploy_game_wine_dll_overrides(game.name, _pfx, game.wine_dll_overrides, log_fn=_log)

    game.save_last_deployed_profile(profile)

    # Deploy Root_Folder
    target_root_folder_dir = game.get_effective_root_folder_path()
    rf_allowed = getattr(game, "root_folder_deploy_enabled", True)

    # Step A: shared Root_Folder first so its log exists before root-flagged mods run.
    if rf_allowed and target_root_folder_dir.is_dir() and game_root:
        count = deploy_root_folder(target_root_folder_dir, game_root,
                                   mode=deploy_mode, log_fn=_log)
        if count:
            _log("Root Folder: transferred files to game root.")

    # Step B: root-flagged mods, merges into Step A log (Root_Folder wins conflicts).
    if game_root:
        _filemap_root_path = staging.parent / "filemap_root.txt"
        _strip = getattr(game, "mod_folder_strip_prefixes", None)
        _rfc = deploy_root_flagged_mods(
            _filemap_root_path, game_root, staging,
            mode=deploy_mode, strip_prefixes=_strip,
            per_mod_strip_prefixes=load_per_mod_strip_prefixes(profile_dir) or None,
            log_fn=_log,
        )
        if _rfc:
            _log(f"Root-flagged mods: {_rfc} file(s) deployed to game root.")

    if hasattr(game, "swap_launcher"):
        game.swap_launcher(_log)

    _log(f"Deploy complete: {game.name} / {profile}")


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

    from Utils.deploy import restore_root_folder

    game_root = game.get_game_path()
    profile_root = game.get_profile_root()

    last_deployed = game.get_last_deployed_profile()
    if last_deployed:
        game.set_active_profile_dir(profile_root / "profiles" / last_deployed)

    if hasattr(game, "restore"):
        game.restore(log_fn=_log)
    else:
        print(f"Game '{game.name}' does not support restore.")

    root_folder_dir = game.get_effective_root_folder_path()
    if root_folder_dir.is_dir() and game_root:
        restore_root_folder(root_folder_dir, game_root, log_fn=_log)

    _log(f"Restore complete: {game.name}")


def main():
    _setup_path()

    parser = argparse.ArgumentParser(
        prog="amethyst",
        description="Amethyst Mod Manager — CLI",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("list-games", help="List all discovered games and whether they are configured")

    lp = subparsers.add_parser("list-profiles", help="List profiles for a game")
    lp.add_argument("game", help="game_id or display name (case-insensitive)")

    dp = subparsers.add_parser("deploy", help="Build filemap and deploy mods for a profile")
    dp.add_argument("game", help="game_id or display name (case-insensitive)")
    dp.add_argument("profile", help="Profile name")

    rp = subparsers.add_parser("restore", help="Restore the game directory (undo last deploy)")
    rp.add_argument("game", help="game_id or display name (case-insensitive)")

    subparsers.add_parser("clear-credentials", help="Remove stored Nexus Mods API key and OAuth tokens")

    args = parser.parse_args()

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
    elif args.command == "restore":
        cmd_restore(games, args.game)


if __name__ == "__main__":
    main()
