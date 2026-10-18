from __future__ import annotations

from pathlib import Path
from typing import Any, Callable


Runner = Callable[[Callable[..., None], Callable[..., None]], dict[str, Any]]


def _run(operation: Callable[[], dict[str, Any]]) -> dict[str, Any]:
    try:
        return operation()
    except Exception as exc:
        from Utils.plugins.manage import PluginStateError
        if not isinstance(exc, PluginStateError):
            raise
        from .jobs import JobError
        raise JobError(exc.name, str(exc), exc.details) from exc


def inventory_runner(game: Any, profile_dir: Path, owner: str) -> Runner:
    def run(log: Callable[..., None], progress: Callable[..., None]) -> dict:
        from Utils.plugins.manage import list_profile_plugins
        return _run(lambda: list_profile_plugins(
            game,
            profile_dir,
            log_fn=log,
            progress_fn=progress,
            operation_owner=owner,
        ))
    return run


def set_enabled_runner(
    game: Any,
    profile_dir: Path,
    changes: list[tuple[str, bool]],
    owner: str,
) -> Runner:
    def run(log: Callable[..., None], progress: Callable[..., None]) -> dict:
        from Utils.plugins.manage import set_plugins_enabled
        return _run(lambda: set_plugins_enabled(
            game,
            profile_dir,
            changes,
            log_fn=log,
            progress_fn=progress,
            operation_owner=owner,
        ))
    return run


def move_runner(
    game: Any,
    profile_dir: Path,
    plugins: list[str],
    anchor: str,
    after: bool,
    owner: str,
) -> Runner:
    def run(log: Callable[..., None], progress: Callable[..., None]) -> dict:
        from Utils.plugins.manage import move_plugins
        return _run(lambda: move_plugins(
            game,
            profile_dir,
            plugins,
            anchor=anchor,
            after=after,
            log_fn=log,
            progress_fn=progress,
            operation_owner=owner,
        ))
    return run


def sort_runner(
    game: Any,
    profile_dir: Path,
    apply_order: bool,
    owner: str,
) -> Runner:
    def run(log: Callable[..., None], progress: Callable[..., None]) -> dict:
        from Utils.plugins.manage import sort_profile_plugins
        return _run(lambda: sort_profile_plugins(
            game,
            profile_dir,
            apply_order=apply_order,
            log_fn=log,
            progress_fn=progress,
            operation_owner=owner,
        ))
    return run
