"""Generation-pinned deployment input backed by :mod:`filegraph`.

Deploy handlers still have different filesystem policies, but they all consume
the same winner generation through this module.  It deliberately has no
``filemap.txt`` fallback: a handler invoked outside the deployment pipeline
gets an actionable error instead of silently deploying stale state.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator

from Utils.filegraph_models import DeployEntry, DeploymentPlan


@dataclass(slots=True)
class ActiveDeployment:
    profile_session: object
    transaction_id: str
    plan: DeploymentPlan
    link_mode: str
    phase: str = "planned"


_local = threading.local()


def begin(profile_session, snapshot_generation: int, link_mode: str) -> ActiveDeployment:
    """Journal and publish a pinned plan for the current deploy thread."""
    if getattr(_local, "deployment", None) is not None:
        raise RuntimeError("a filegraph deployment is already active on this thread")
    transaction_id, plan = profile_session.begin_deployment(
        snapshot_generation, link_mode)
    active = ActiveDeployment(profile_session, transaction_id, plan, link_mode)
    _local.deployment = active
    return active


def current() -> ActiveDeployment | None:
    return getattr(_local, "deployment", None)


def require_active() -> ActiveDeployment:
    active = current()
    if active is None:
        raise RuntimeError(
            "No pinned filegraph deployment plan is active. Start deployment "
            "through Amethyst's deploy pipeline; legacy filemap.txt input is "
            "not supported."
        )
    return active


def mark_phase(phase: str) -> None:
    """Durably advance the recovery journal before the next mutation class."""
    active = require_active()
    if active.phase == phase:
        return
    active.profile_session.update_deployment_phase(
        active.transaction_id, phase)
    active.phase = phase


def finish(*, success: bool) -> None:
    """Commit success or leave an incomplete journal for startup recovery."""
    active = require_active()
    try:
        if success:
            mark_phase("database_commit")
            active.profile_session.commit_deployment(active.transaction_id)
    finally:
        try:
            del _local.deployment
        except AttributeError:
            pass


def input_ready() -> bool:
    """Whether a handler can consume the pinned in-memory deployment input."""
    return current() is not None


def entries(
    *,
    include_root: bool = False,
    targets: Iterable[str] = (),
) -> Iterator[DeployEntry]:
    """Iterate the pinned deploy winners without copying the plan."""
    allowed_targets = set(targets)
    for entry in require_active().plan.entries:
        is_root = entry.legacy_root or entry.provider_kind == "root"
        if is_root != include_root:
            continue
        if allowed_targets and entry.target not in allowed_targets:
            continue
        yield entry


def legacy_rows(*, root: bool = False) -> tuple[tuple[str, str], ...]:
    """Compatibility row projection for handlers during their migration.

    This is an in-memory projection of one immutable snapshot, not a legacy
    file read or an independently resolved map.
    """
    rows = []
    for entry in entries(include_root=root):
        if entry.mod_name == "[Root_Folder]" or not entry.legacy_rel:
            continue
        rows.append((entry.legacy_rel, entry.mod_name))
    return tuple(rows)


def legacy_lines(*, root: bool = False) -> tuple[str, ...]:
    return tuple(f"{relative}\t{mod_name}" for relative, mod_name in legacy_rows(root=root))


def deployed_entries_for(game, profile_dir: Path):
    """Read the last successfully committed deployment for restore/recovery."""
    from Utils.filegraph_service import FileGraphService
    library = FileGraphService.open_library(game, profile_dir)
    profile = library.open_profile(profile_dir)
    cached = profile.cached_deployment_plan()
    return cached.entries if cached is not None else profile.deployed_entries()


def absolute_destination(game, entry) -> Path | None:
    """Resolve a catalog target domain without consulting a legacy map."""
    if entry.target == "game":
        root = game.get_game_path()
    elif entry.target == "prefix":
        root = game.get_prefix_path()
    elif entry.target.startswith("custom:"):
        root = Path(entry.target[len("custom:"):])
    else:
        return None
    if root is None:
        return None
    return Path(root) / entry.destination


def entry_relative_to(
    game, entry, root: Path, target_roots: dict[str, str] | None = None,
) -> str | None:
    """Fast string projection of one catalog destination below ``root``.

    Bulk deploy/restore callers pass a shared ``target_roots`` cache. This
    avoids calling game path getters and allocating two Path objects for every
    deployed entry while retaining Path.relative_to's strict prefix semantics.
    """
    roots = target_roots if target_roots is not None else {}
    target_root = roots.get(entry.target)
    if target_root is None:
        if entry.target == "game":
            value = game.get_game_path()
        elif entry.target == "prefix":
            value = game.get_prefix_path()
        elif entry.target.startswith("custom:"):
            value = entry.target[len("custom:"):]
        else:
            return None
        if value is None:
            return None
        target_root = str(value).rstrip("/")
        roots[entry.target] = target_root
    destination = target_root + "/" + entry.destination.lstrip("/")
    root_string = str(root).rstrip("/")
    prefix = root_string + "/"
    if not destination.startswith(prefix):
        return None
    relative = destination[len(prefix):]
    if not relative or relative.startswith("/"):
        return None
    from Utils.path_utils import has_path_traversal
    return None if has_path_traversal(relative) else relative


def deployed_paths_below(game, profile_dir: Path, root: Path) -> tuple[str, ...]:
    root = Path(root)
    result = []
    for entry in deployed_entries_for(game, profile_dir):
        destination = absolute_destination(game, entry)
        if destination is None:
            continue
        try:
            result.append(destination.relative_to(root).as_posix())
        except ValueError:
            continue
    return tuple(result)
