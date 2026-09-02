"""Incremental physical deployment backed by Filegraph committed state."""

from __future__ import annotations

import errno
import json
import os
import stat as _stat
from dataclasses import dataclass
from pathlib import Path

from Utils.app_log import safe_log as _safe_log
from Utils.deployment.shared import (
    LinkMode,
    _append_overwrite_log,
    _default_core,
    _do_link_ex,
    _fallback_snapshot,
    _report_fallbacks,
    _iter_map_batched,
    _mkdir_leaves,
    _move_crash_safe,
    _resolve_root_path_str,
)


_DELTA_FALLBACK_RATIO = 0.40


class IncrementalFallback(RuntimeError):
    """A retained deployment cannot safely continue in place."""


def incremental_enabled() -> bool:
    return os.environ.get("AMM_DEPLOY_INCREMENTAL") != "0"


def verify_enabled() -> bool:
    return os.environ.get("AMM_DEPLOY_VERIFY") == "1"


@dataclass(slots=True)
class IncrementalPlan:
    game: object
    deploy_dir_str: str
    core_dir: Path
    state_dir: Path
    mode: LinkMode
    old_entries: dict
    deploy_stats: dict | None
    new_entries: dict | None = None
    ran_incremental: bool = False
    profile_session: object | None = None
    projection_cache_key: str | None = None


_ACTIVE: IncrementalPlan | None = None


def activate(plan: IncrementalPlan) -> None:
    global _ACTIVE
    _ACTIVE = plan


def deactivate() -> None:
    global _ACTIVE
    _ACTIVE = None


def is_active() -> bool:
    return _ACTIVE is not None


def active_for(deploy_dir) -> IncrementalPlan | None:
    if _ACTIVE is None:
        return None
    if str(deploy_dir) != _ACTIVE.deploy_dir_str:
        raise IncrementalFallback(
            f"unexpected deploy target {deploy_dir} during an incremental "
            f"deploy of {_ACTIVE.deploy_dir_str}")
    return _ACTIVE


def _entry_relative(
    game, entry, deploy_dir: Path, target_roots: dict[str, str] | None = None,
) -> str | None:
    from Utils.filegraph.deploy import entry_relative_to
    return entry_relative_to(game, entry, deploy_dir, target_roots)


def _project_entries(game, entries, deploy_dir: Path) -> dict | None:
    """Project exact catalog destinations into one standard deploy root."""
    result = {}
    target_roots: dict[str, str] = {}
    for entry in entries:
        if entry.provider_kind == "root" or getattr(entry, "legacy_root", False):
            # Root_Folder/root-flagged deployment has its own restore/deploy
            # phase in the pipeline and is not part of the retained Data tree.
            continue
        relative = _entry_relative(game, entry, deploy_dir, target_roots)
        if relative is None:
            # Custom/prefix/game-root routes are likewise restored and placed
            # by their dedicated handlers. The standard handler's task-set
            # equality check below remains the final guard: an unrecognised
            # custom placement forces a safe full-path fallback.
            continue
        key = relative.lower()
        if key in result:
            return None
        result[key] = entry
    return result


def plan_incremental(
    game,
    profile: str,
    mode: LinkMode,
    profile_session,
    log_fn=None,
) -> IncrementalPlan | None:
    """Validate the retained deployment without reading a legacy map."""
    log = _safe_log(log_fn)
    if not incremental_enabled() or not getattr(
            game, "supports_incremental_deploy", False):
        return None

    def skip(reason: str):
        log(f"Incremental deploy unavailable - {reason}; using the full path.")
        return None

    try:
        if game.get_last_deployed_profile() != profile:
            return skip("a different profile was deployed last")
        if not game.get_deploy_active():
            return skip("no active deployment")
        if game.get_last_deploy_mode() != mode.name:
            return skip("deploy mode changed")
        if profile_session is None or profile_session.incomplete_operations():
            return skip("deployment recovery is pending")
        deploy_dir = game.get_mod_data_path()
        if deploy_dir is None or not Path(deploy_dir).is_dir():
            return skip("no deploy directory")
        deploy_dir = Path(deploy_dir)
        from Utils.deployment.standard import (
            _DEPLOY_MARKER_NAME,
            _DEPLOY_STATS_NAME,
        )
        core_dir = _default_core(deploy_dir)
        if not (deploy_dir / _DEPLOY_MARKER_NAME).is_file():
            return skip("deploy marker missing")
        if not core_dir.is_dir():
            return skip(f"{core_dir.name}/ backup missing")
        state_dir = game.get_effective_filemap_path().parent
        stats_path = state_dir / _DEPLOY_STATS_NAME
        if not stats_path.is_file():
            return skip("previous deploy statistics missing")
        if (state_dir / "custom_deploy_log.txt").is_file():
            return skip("custom-location recovery is pending")
        from Utils.deployment.shared import load_separator_deploy_paths
        profile_dir = game.get_profile_root() / "profiles" / profile
        for info in (load_separator_deploy_paths(profile_dir) or {}).values():
            if not isinstance(info, dict):
                continue
            if info.get("path") or info.get("raw") or info.get("merge"):
                return skip("separator deployment overrides are configured")
            if (info.get("mode") or "").strip().lower() in (
                    "hardlink", "symlink"):
                return skip("separator link-mode overrides are configured")
        cached_plan = profile_session.cached_deployment_plan(mode.name.lower())
        deployed = (cached_plan.entries if cached_plan is not None
                    else profile_session.deployed_entries())
        projection_cache_key = f"standard:{deploy_dir}"
        old_entries = (
            profile_session.cached_deployment_projection(
                projection_cache_key, cached_plan)
            if cached_plan is not None else None
        )
        if old_entries is None:
            old_entries = _project_entries(game, deployed, deploy_dir)
            if cached_plan is not None and old_entries is not None:
                profile_session.cache_deployment_projection(
                    projection_cache_key, cached_plan, old_entries)
        if not old_entries:
            return skip("committed deployed state is empty or not Data-only")
        return IncrementalPlan(
            game=game,
            deploy_dir_str=str(deploy_dir),
            core_dir=core_dir,
            state_dir=state_dir,
            mode=mode,
            old_entries=old_entries,
            # Hardlinks and symlinks can be identified without this large
            # file.  Load it lazily only if a copy-mode destination actually
            # needs its recorded fingerprint.
            deploy_stats=None,
            profile_session=profile_session,
            projection_cache_key=projection_cache_key,
        )
    except Exception as exc:
        return skip(f"eligibility check failed ({exc})")


def bind_deployment_plan(plan: IncrementalPlan, deployment_plan) -> None:
    """Bind the newly reconciled generation before any filesystem mutation."""
    projected = _project_entries(
        plan.game, deployment_plan.entries, Path(plan.deploy_dir_str))
    if projected is None:
        raise IncrementalFallback(
            "the new plan contains root, prefix, or custom destinations")
    plan.new_entries = projected
    if plan.profile_session is not None and plan.projection_cache_key is not None:
        plan.profile_session.cache_deployment_projection(
            plan.projection_cache_key, deployment_plan, projected)


def deployment_unchanged(
    profile_session,
    snapshot_generation: int,
    link_mode: str,
) -> bool:
    """Whether the pinned plan exactly matches committed deployed state.

    This is the zero-I/O deployment fast path. It deliberately refuses to
    answer true while a recovery journal is outstanding.
    """
    if not incremental_enabled():
        return False
    return profile_session.deployment_unchanged(
        snapshot_generation, str(link_mode).lower())


def _entry_signature(entry, link_mode: str) -> tuple:
    return (
        entry.mod_key,
        entry.provider_kind,
        entry.source_rel,
        entry.source_display,
        entry.source_fingerprint,
        str(link_mode).lower(),
    )


def apply_incremental(
    plan: IncrementalPlan,
    tasks: list,
    rel_mod: dict[str, tuple[str, str]],
    *,
    deploy_dir: Path,
    core_dir: Path,
    overwrite_dir: Path,
    mode: LinkMode,
    state_dir: Path,
    staging_root: Path | None = None,
    excluded_plan_keys: set[str] | None = None,
    log_fn=None,
    progress_fn=None,
) -> tuple[int, set[str]]:
    """Apply only changed destinations from two catalog generations."""
    del state_dir, staging_root
    log = _safe_log(log_fn)
    if plan.new_entries is None:
        raise IncrementalFallback("the new Filegraph plan was not bound")
    if any(task[3] for task in tasks):
        raise IncrementalFallback("custom-location tasks are present")
    if Path(core_dir) != plan.core_dir or mode is not plan.mode:
        raise IncrementalFallback("deployment target or link mode changed")

    new_tasks = {task[2]: task for task in tasks}
    excluded = excluded_plan_keys or set()
    old = {
        key: entry for key, entry in plan.old_entries.items()
        if key not in excluded
    }
    new = {
        key: entry for key, entry in plan.new_entries.items()
        if key not in excluded
    }
    if set(new_tasks) != set(new):
        raise IncrementalFallback(
            "the handler task projection differs from the pinned Filegraph plan")
    old_keys = set(old)
    new_keys = set(new)
    removed = old_keys - new_keys
    added = new_keys - old_keys
    changed = {
        key for key in old_keys & new_keys
        if _entry_signature(
            old[key], getattr(old[key], "link_mode", None) or plan.mode.name)
        != _entry_signature(new[key], mode.name)
    }
    relink = added | changed
    delta = len(removed) + len(relink)
    scale = max(len(old), len(new), 1)
    if delta > _DELTA_FALLBACK_RATIO * scale:
        raise IncrementalFallback(
            f"too many changed destinations ({delta} of {scale})")

    log(
        f"  Incremental: {len(added)} added, {len(removed)} removed, "
        f"{len(changed)} replaced, {len(new_keys - relink)} unchanged."
    )
    from Utils.filegraph.deploy import current as current_deployment, mark_phase
    if current_deployment() is not None:
        mark_phase("removing")
    deploy_dir_str = str(deploy_dir)
    prefix_length = len(deploy_dir_str) + 1
    stats = plan.deploy_stats

    def recorded_stat(relative_key: str):
        nonlocal stats
        if stats is None:
            from Utils.deployment.standard import (
                _DEPLOY_STATS_NAME, _load_deploy_stats,
            )
            stats = _load_deploy_stats(
                plan.state_dir / _DEPLOY_STATS_NAME)
        return stats.get(relative_key)
    listing_cache: dict[str, dict[str, str]] = {}
    resolved_cache: dict[str, str] = {}
    core_listing_cache: dict[str, dict[str, str]] = {}
    core_resolved_cache: dict[str, str] = {}

    def old_relative(key: str) -> str:
        value = _entry_relative(plan.game, old[key], deploy_dir)
        if value is None:
            raise IncrementalFallback(
                f"committed destination escaped deploy root: {key}")
        return value

    def core_lookup(relative: str) -> str | None:
        path = _resolve_root_path_str(
            str(core_dir), relative, core_listing_cache,
            resolved_dir_cache=core_resolved_cache)
        return path if os.path.lexists(path) else None

    rescued_overwrite: list[str] = []
    rescued_to_mod = 0

    def clear_destination(
        destination: str,
        relative_key: str,
        relative_display: str,
        staging_destination: str | None = None,
    ) -> None:
        nonlocal rescued_to_mod
        try:
            deployed_stat = os.lstat(destination)
        except OSError:
            return
        if _stat.S_ISLNK(deployed_stat.st_mode):
            os.unlink(destination)
            return
        if not _stat.S_ISREG(deployed_stat.st_mode):
            raise IncrementalFallback(
                f"unexpected non-file entry at {destination}")
        if deployed_stat.st_nlink > 1:
            os.unlink(destination)
            return
        recorded = recorded_stat(relative_key)
        from Utils.deployment.standard import _MTIME_TOLERANCE_NS
        if (recorded is not None
                and deployed_stat.st_size == recorded[0]
                and abs(deployed_stat.st_mtime_ns - recorded[1])
                <= _MTIME_TOLERANCE_NS):
            os.unlink(destination)
            return
        vanilla = core_lookup(relative_display)
        if vanilla is not None:
            try:
                vanilla_stat = os.lstat(vanilla)
                if (
                    (deployed_stat.st_dev == vanilla_stat.st_dev
                     and deployed_stat.st_ino == vanilla_stat.st_ino)
                    or (
                        deployed_stat.st_size == vanilla_stat.st_size
                        and abs(deployed_stat.st_mtime_ns
                                - vanilla_stat.st_mtime_ns)
                        <= _MTIME_TOLERANCE_NS
                    )
                ):
                    os.unlink(destination)
                    return
            except OSError:
                pass
        if staging_destination is not None:
            _move_crash_safe(destination, staging_destination)
            rescued_to_mod += 1
            return
        overwrite_destination = str(overwrite_dir / relative_display)
        _move_crash_safe(destination, overwrite_destination)
        rescued_overwrite.append(relative_display)

    refill_tasks: list[tuple[str, str]] = []
    prune_dirs: set[str] = set()
    for key in removed:
        relative = old_relative(key)
        destination = _resolve_root_path_str(
            deploy_dir_str, relative, listing_cache,
            resolved_dir_cache=resolved_cache)
        clear_destination(destination, key, relative)
        prune_dirs.add(os.path.dirname(destination))
        vanilla = core_lookup(relative)
        if vanilla is not None:
            refill_tasks.append((vanilla, destination))

    for key in relink:
        task = new_tasks[key]
        old_destination = None
        old_display = task[1][prefix_length:]
        if key in old:
            old_display = old_relative(key)
            old_destination = _resolve_root_path_str(
                deploy_dir_str, old_display, listing_cache,
                resolved_dir_cache=resolved_cache)
        new_destination = task[1]
        staging_destination = task[0] if key in changed else None
        if old_destination is not None:
            clear_destination(
                old_destination, key, old_display, staging_destination)
        if old_destination != new_destination:
            clear_destination(
                new_destination, key, task[1][prefix_length:],
                staging_destination)

    def effective_mode(task) -> LinkMode:
        if task[4]:
            return LinkMode.SYMLINK
        return task[5] if task[5] is not None else mode

    link_specs = [
        (new_tasks[key][0], new_tasks[key][1], key,
         effective_mode(new_tasks[key]))
        for key in relink
    ]
    required_dirs = {os.path.dirname(item[1]) for item in link_specs}
    required_dirs.update(os.path.dirname(item[1]) for item in refill_tasks)
    _mkdir_leaves(required_dirs)
    if current_deployment() is not None:
        mark_phase("placing")
    linked = 0
    completed = 0
    fallback_before = _fallback_snapshot()
    mode_counts: dict[LinkMode, int] = {}
    total_operations = len(link_specs) + len(refill_tasks)
    placed_relinked: set[str] = set()
    new_stats: dict[str, tuple[str, int, int]] = {}

    def place(spec):
        source, destination, key, transfer_mode = spec
        actual, error = _do_link_ex(source, destination, transfer_mode)
        stat_record = None
        if error is None and actual is not LinkMode.SYMLINK:
            try:
                placed_stat = os.lstat(destination)
                if _stat.S_ISREG(placed_stat.st_mode):
                    stat_record = (
                        destination[prefix_length:], placed_stat.st_size,
                        placed_stat.st_mtime_ns,
                    )
            except OSError:
                pass
        return key, actual, error, destination, stat_record

    def fatal_place(result) -> bool:
        return result[2] is not None and getattr(
            result[2], "errno", None) == errno.ENOSPC

    for key, actual, error, destination, stat_record in _iter_map_batched(
            place, link_specs, stop_on=fatal_place):
        completed += 1
        if error is not None:
            if getattr(error, "errno", None) == errno.ENOSPC:
                raise OSError(errno.ENOSPC, f"game drive full at {destination}")
            log(f"  WARN: could not transfer {destination}: {error}")
        else:
            linked += 1
            if actual is not None:
                mode_counts[actual] = mode_counts.get(actual, 0) + 1
            placed_relinked.add(key)
            if stat_record is not None:
                new_stats[key] = stat_record
        if progress_fn is not None and (
                completed % 200 == 0 or completed == total_operations):
            progress_fn(completed, total_operations)

    def refill(item):
        source, destination = item
        actual, error = _do_link_ex(source, destination, mode)
        return destination, actual, error

    for destination, actual, error in _iter_map_batched(
            refill, refill_tasks,
            stop_on=lambda result: getattr(
                result[2], "errno", None) == errno.ENOSPC):
        completed += 1
        if error is not None:
            if getattr(error, "errno", None) == errno.ENOSPC:
                raise OSError(errno.ENOSPC, f"game drive full at {destination}")
            log(f"  WARN: could not restore vanilla {destination}: {error}")
        elif actual is not None:
            mode_counts[actual] = mode_counts.get(actual, 0) + 1
        if progress_fn is not None and (
                completed % 200 == 0 or completed == total_operations):
            progress_fn(completed, total_operations)

    for directory in sorted(
            prune_dirs, key=lambda value: value.count("/"), reverse=True):
        current = directory
        while current != deploy_dir_str and current.startswith(
                deploy_dir_str + "/"):
            try:
                os.rmdir(current)
            except OSError:
                break
            current = os.path.dirname(current)

    if rescued_to_mod:
        log(f"  Rescued {rescued_to_mod} edited file(s) back to mod folders.")
    if rescued_overwrite:
        log(
            f"  Rescued {len(rescued_overwrite)} runtime/edited file(s) "
            "to overwrite/."
        )
        _append_overwrite_log(overwrite_dir, rescued_overwrite, log)

    from Utils.deployment.standard import _report_mode_breakdown
    _report_mode_breakdown(log, mode_counts, mode)
    _report_fallbacks(log, fallback_before)

    final_placed = (new_keys - relink) | placed_relinked
    from Utils.deployment.standard import (
        _DEPLOY_STATS_NAME, _write_deploy_stats_delta,
    )
    # Keep the large full-deploy file as an immutable baseline and record only
    # destinations touched by this deployment.  Tombstones suppress stale
    # baseline records for removed, failed, and symlinked destinations.
    stats_updates: dict[str, tuple[str, int | None, int | None]] = {}
    for key in removed:
        stats_updates[key] = (old_relative(key), None, None)
    for key in relink:
        stats_updates[key] = (
            new_tasks[key][1][prefix_length:], None, None)
    stats_updates.update(new_stats)
    _write_deploy_stats_delta(
        plan.state_dir / _DEPLOY_STATS_NAME, stats_updates, log_fn=log_fn)
    plan.ran_incremental = True
    return linked, final_placed


def plan_vfs_redeploy(game, profile: str, log_fn=None) -> bool:
    """Return whether an existing profile VFS view can be replaced in place."""
    if not incremental_enabled():
        return False
    if not getattr(game, "vfs_launch_enabled", False):
        return False

    log = _safe_log(log_fn)

    def skip(reason: str) -> bool:
        log(
            f"Incremental VFS deploy unavailable - {reason}; "
            "using the full path."
        )
        return False

    try:
        if game.get_last_deployed_profile() != profile:
            return skip("a different profile was deployed last")
        if not game.get_deploy_active():
            return skip("no active deployment")
        if game.get_last_deploy_mode() != "VFS":
            return skip("the previous deployment was not VFS")

        data_root_getter = getattr(game, "get_vfs_data_root", None)
        data_root = (
            data_root_getter() if callable(data_root_getter)
            else game.get_mod_data_path()
        )
        if data_root is not None and _default_core(Path(data_root)).is_dir():
            return skip("physical deployment recovery state is still present")

        from Utils.vfs import BACKEND_SHADOW, manifest_path
        manifest = manifest_path(game, profile)
        if not manifest.is_file():
            return skip("published view manifest missing")
        payload = json.loads(manifest.read_text(encoding="utf-8"))
        if (not isinstance(payload, dict)
                or payload.get("backend") != BACKEND_SHADOW):
            return skip("the published view uses an older backend")
        if payload.get("profile") != profile:
            return skip("published view belongs to a different profile")
        return True
    except Exception as exc:
        return skip(f"eligibility check failed ({exc})")


__all__ = [
    "IncrementalFallback", "IncrementalPlan", "active_for", "activate",
    "apply_incremental", "bind_deployment_plan", "deactivate",
    "deployment_unchanged", "incremental_enabled", "is_active",
    "plan_incremental", "plan_vfs_redeploy", "verify_enabled",
]
