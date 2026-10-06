"""Materialize instruction routes through the shared routing journal."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from Utils.deployment.vortex import FLAG_VORTEX_ROUTE


_STATE = ".vortex-routing"


def _journals(game):
    metadata = game.get_effective_filemap_path().parent / _STATE
    if metadata.is_symlink():
        raise RuntimeError(f"Vortex routing journal must not be a symlink: {metadata}")
    for name, root, prefix in (
        ("game", game.get_game_path() or game.get_prefix_path(), game.get_prefix_path()),
        ("data", game.get_mod_data_path(), None),
    ):
        folder = metadata / name
        if folder.is_symlink():
            raise RuntimeError(f"Vortex routing journal must not be a symlink: {folder}")
        if not folder.is_dir() or not any(folder.iterdir()):
            continue
        if root is None:
            from Utils.deployment.shared import RestoreIncompleteError
            raise RestoreIncompleteError(
                f"Configure the original {name} destination to restore {folder}.")
        yield folder / "catalog-input", Path(root), prefix


def validate_restore(game, log_fn=None):
    from Utils.deployment.custom_rules import validate_custom_rules_restore
    for filemap, root, prefix in _journals(game):
        validate_custom_rules_restore(filemap, root, prefix, log_fn)


def restore_routes(game, log_fn=None):
    from Utils.deployment.custom_rules import restore_custom_rules
    journals = list(_journals(game))
    validate_restore(game, log_fn)
    for filemap, root, prefix in journals:
        restore_custom_rules(filemap, root, [], log_fn=log_fn, prefix_root=prefix)
        try:
            filemap.parent.rmdir()
            filemap.parent.parent.rmdir()
        except OSError:
            pass


def validate_routes(game):
    from Utils.filegraph.deploy import absolute_destination, require_active
    from Utils.deployment.vortex import relative_path
    rows = [row for row in require_active().plan.entries if row.flags & FLAG_VORTEX_ROUTE]
    for row in rows:
        relative_path(row.destination)
        destination = absolute_destination(game, row)
        if destination is None:
            raise RuntimeError(
                f"Vortex instructions require a configured {row.target} destination.")
        source = row.source_path
        if source is None or not source.is_file():
            raise RuntimeError(f"Missing Vortex source: {row.source_display}")
        if not source.resolve().is_relative_to(row.source_root.resolve()):
            raise RuntimeError(f"Vortex source escapes the mod directory: {source}")
        if row.target not in {"prefix", "game"}:
            root = game.get_mod_data_path()
            if root is None or row.target != "custom:" + str(Path(root).resolve()):
                raise RuntimeError("Unexpected Vortex custom destination.")
    return rows


def deploy_routes(game, mode, log_fn=None, *, game_layer=None, data_layer=None,
                  temporary_state=None):
    from Utils.deployment.custom_rules import deploy_custom_rules
    from Utils.deployment.shared import LinkMode
    rows = validate_routes(game)
    if not rows:
        return 0
    prefix_rows = [row for row in rows if row.target == "prefix"]
    validate_restore(game, log_fn)
    from Utils.deployment.shared import load_separator_deploy_paths, expand_separator_link_modes
    from Utils.mods.modlist import read_modlist
    profile = (getattr(game, "_active_profile_dir", None)
               or game.get_effective_filemap_path().parent)
    modes = expand_separator_link_modes(load_separator_deploy_paths(profile),
                                        read_modlist(profile / "modlist.txt"))
    state = game.get_effective_filemap_path().parent / _STATE
    groups = [("game", [row for row in rows if row.target == "game"],
               game_layer or game.get_game_path(), game_layer is not None),
              ("prefix", prefix_rows, game.get_game_path() or game.get_prefix_path(), False),
              ("data", [row for row in rows if row.target.startswith("custom:")],
               data_layer or game.get_mod_data_path(), data_layer is not None)]
    physical_game = []
    for name, selected, root, temporary in groups:
        if not selected:
            continue
        if root is None:
            raise RuntimeError(f"Vortex instructions have no configured {name} destination.")
        if name == "data":
            expected = "custom:" + str(Path(game.get_mod_data_path()).resolve())
            if any(row.target != expected for row in selected):
                raise RuntimeError("Unexpected Vortex custom destination.")
            selected = [replace(row, target="game") for row in selected]
        if name in {"game", "prefix"} and not temporary:
            physical_game.extend(selected)
            continue
        folder = ((Path(temporary_state) / name) if temporary else state / name)
        folder.mkdir(parents=True, exist_ok=True)
        deploy_custom_rules(
            folder / "catalog-input", Path(root), game.get_effective_mod_staging_path(), [],
            mode=LinkMode.HARDLINK if temporary else mode, log_fn=log_fn,
            prefix_root=game.get_prefix_path(), resolved_entries=selected,
            per_mod_link_modes={} if temporary else modes)
    if physical_game:
        folder = state / "game"
        folder.mkdir(parents=True, exist_ok=True)
        deploy_custom_rules(
            folder / "catalog-input", game.get_game_path() or game.get_prefix_path(),
            game.get_effective_mod_staging_path(), [], mode=mode, log_fn=log_fn,
            prefix_root=game.get_prefix_path(), resolved_entries=physical_game,
            per_mod_link_modes=modes)
    return len(rows)
