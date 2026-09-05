from __future__ import annotations

from pathlib import Path
from typing import Any, Callable


Runner = Callable[[Callable[..., None], Callable[..., None]], dict[str, Any]]


def _run_boundary(operation: Callable[[], dict]) -> dict:
    from Utils.boundaries.manage import BoundaryError
    try:
        return operation()
    except BoundaryError as exc:
        from .jobs import JobError
        raise JobError(exc.name, str(exc), exc.details) from exc


def inventory_runner(
    game: Any,
    profile_dir: Path,
    boundary: str,
    offset: int,
    limit: int,
    owner: str,
) -> Runner:
    def run(log: Callable[..., None], progress: Callable[..., None]) -> dict:
        from Utils.boundaries.manage import list_profile_boundary
        return _run_boundary(
            lambda: list_profile_boundary(
                game,
                profile_dir,
                boundary,
                offset=offset,
                limit=limit,
                log_fn=log,
                progress_fn=progress,
                operation_owner=owner,
            )
        )
    return run


def move_runner(
    game: Any,
    profile_dir: Path,
    boundary: str,
    paths: list[str],
    revision: str,
    target_mod: str,
    create_mod: bool,
    owner: str,
) -> Runner:
    def run(log: Callable[..., None], progress: Callable[..., None]) -> dict:
        from Utils.boundaries.manage import move_profile_boundary
        return _run_boundary(
            lambda: move_profile_boundary(
                game,
                profile_dir,
                boundary,
                paths,
                revision,
                target_mod,
                create_mod=create_mod,
                log_fn=log,
                progress_fn=progress,
                operation_owner=owner,
            )
        )
    return run


def remove_runner(
    game: Any,
    profile_dir: Path,
    boundary: str,
    paths: list[str],
    revision: str,
    owner: str,
) -> Runner:
    def run(log: Callable[..., None], progress: Callable[..., None]) -> dict:
        from Utils.boundaries.manage import remove_profile_boundary
        return _run_boundary(
            lambda: remove_profile_boundary(
                game,
                profile_dir,
                boundary,
                paths,
                revision,
                log_fn=log,
                progress_fn=progress,
                operation_owner=owner,
            )
        )
    return run
