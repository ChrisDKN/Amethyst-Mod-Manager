from __future__ import annotations

from pathlib import Path
from typing import Any, Callable


Runner = Callable[[Callable[..., None], Callable[..., None]], dict[str, Any]]


def inventory_runner(
    game: Any,
    profile_dir: Path,
    boundary: str,
    offset: int,
    limit: int,
    owner: str,
) -> Runner:
    def run(log: Callable[..., None], progress: Callable[..., None]) -> dict:
        from Utils.boundaries.manage import BoundaryError, list_profile_boundary
        try:
            return list_profile_boundary(
                game,
                profile_dir,
                boundary,
                offset=offset,
                limit=limit,
                log_fn=log,
                progress_fn=progress,
                operation_owner=owner,
            )
        except BoundaryError as exc:
            from .jobs import JobError
            raise JobError(exc.name, str(exc), exc.details) from exc
    return run
