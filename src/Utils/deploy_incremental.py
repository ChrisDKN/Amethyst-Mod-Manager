"""
deploy_incremental.py
Incremental redeploy for standard-mode (Data/) games.

When nothing structural changed since the last deploy — same profile, same
link mode, deployment still on disk — the deploy pipeline activates an
"incremental plan" and the three standard primitives cooperate instead of
tearing everything down and rebuilding it:

  move_to_core    — keeps the deployed Data/ and its <Data>_Core backup
  deploy_filemap  — diffs the new task set against the previous deploy
                    (apply_incremental below) and only unlinks/links deltas
  deploy_core     — no-op; the diff refills vanilla gaps itself

The handler's own post-deploy steps (plugins.txt symlink, archive
invalidation, plugin mtime stamping, …) run unchanged.  Any anomaly raises
IncrementalFallback, which run_deploy_pipeline catches to rerun the classic
full restore + deploy — restore_data_core is built to recover an arbitrary
half-mutated Data/, so a partial incremental pass is always safe to abandon.

Kill switch: AMM_DEPLOY_INCREMENTAL=0 forces the full path.
Verify mode:  AMM_DEPLOY_VERIFY=1 re-checks every deployed file after an
incremental deploy and logs mismatches (never fails the deploy).

State on disk (all in the profile root, beside filemap.txt):
  deployed_filemap.txt — rel→mod of the last deploy's effective placed set,
                         written by every successful deploy_filemap run
  deploy_stats.txt     — (size, mtime_ns) per placed regular file (existing)
  last_deploy_mode     — key in deploy_state.json (see BaseGame)
"""

from __future__ import annotations

import concurrent.futures
import errno
import os
import stat as _stat_m
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from typing import Mapping

from Utils.app_log import safe_log as _safe_log
from Utils.atomic_write import atomic_writer
from Utils.deploy_shared import (
    LinkMode,
    _OVERWRITE_NAME,
    _append_overwrite_log,
    _default_core,
    _deploy_workers,
    _do_link_ex,
    _map_batched,
    _mkdir_leaves,
    _move_crash_safe,
    _resolve_root_path_str,
    resolve_mod_dir,
)

DEPLOYED_FILEMAP_NAME = "deployed_filemap.txt"

# When more than this share of the (old ∪ new) deployment changed, a full
# deploy is cheaper/safer than a diff — mirrors filemap.py's incremental cap.
_DELTA_FALLBACK_RATIO = 0.40


class IncrementalFallback(RuntimeError):
    """The incremental fast path cannot (or should not) proceed.

    Raised by the primitive hooks / apply_incremental and caught in exactly
    one place: run_deploy_pipeline, which then reruns the full restore +
    deploy.  Safe even after partial mutation — restore_data_core recovers
    any intermediate Data/ state (fresh links are symlinks / nlink>1 files,
    rescued runtime files are already in overwrite/)."""


def incremental_enabled() -> bool:
    """Kill switch — set AMM_DEPLOY_INCREMENTAL=0 to force full deploys."""
    return os.environ.get("AMM_DEPLOY_INCREMENTAL") != "0"


def verify_enabled() -> bool:
    """AMM_DEPLOY_VERIFY=1 → re-check the deployed tree after the diff."""
    return os.environ.get("AMM_DEPLOY_VERIFY") == "1"


@dataclass
class IncrementalPlan:
    """Everything the primitives need to run one incremental deploy."""
    deploy_dir_str: str
    core_dir: Path
    state_dir: Path                 # profile root holding deploy_stats.txt etc.
    mode: LinkMode
    old_filemap: dict               # rel_lower -> (rel_str, mod_name)
    deploy_stats: dict              # rel_lower -> (size, mtime_ns)
    ran_incremental: bool = False   # set once apply_incremental completed


# Exactly one deploy runs at a time (the Qt app coalesces Deploy requests and
# the CLI is single-threaded), so a module global is sufficient.
_ACTIVE: "IncrementalPlan | None" = None


def activate(plan: IncrementalPlan) -> None:
    global _ACTIVE
    _ACTIVE = plan


def deactivate() -> None:
    global _ACTIVE
    _ACTIVE = None


def is_active() -> bool:
    return _ACTIVE is not None


def active_for(deploy_dir) -> "IncrementalPlan | None":
    """Return the active plan when *deploy_dir* is its target, else None.

    A primitive invoked for a *different* directory while a plan is active
    (e.g. a chained second deploy target) is a state we never planned for —
    fall back to the full path rather than guess."""
    if _ACTIVE is None:
        return None
    if str(deploy_dir) != _ACTIVE.deploy_dir_str:
        raise IncrementalFallback(
            f"unexpected deploy target {deploy_dir} during an incremental "
            f"deploy of {_ACTIVE.deploy_dir_str}")
    return _ACTIVE


# ---------------------------------------------------------------------------
# deployed_filemap.txt
# ---------------------------------------------------------------------------

def write_deployed_filemap(path: Path, entries, log_fn=None) -> None:
    """Atomically write the deployed rel→mod record (one entry per line)."""
    try:
        with atomic_writer(path, "w", errors="surrogateescape") as fh:
            fh.write("# deployed_filemap v1\n")
            for rel_str, mod_name in entries:
                fh.write(f"{rel_str}\t{mod_name}\n")
    except OSError as exc:
        _safe_log(log_fn)(f"  WARN: could not write deployed filemap: {exc}")


def load_deployed_filemap(path: Path) -> "dict[str, tuple[str, str]]":
    """Read deployed_filemap.txt into {rel_lower: (rel_str, mod)}; {} if absent."""
    out: dict[str, tuple[str, str]] = {}
    try:
        with path.open(encoding="utf-8", errors="surrogateescape") as fh:
            for line in fh:
                if line.startswith("#") or "\t" not in line:
                    continue
                rel_str, mod_name = line.rstrip("\n").split("\t", 1)
                out[rel_str.lower()] = (rel_str, mod_name)
    except OSError:
        return {}
    return out


# ---------------------------------------------------------------------------
# Eligibility
# ---------------------------------------------------------------------------

def plan_incremental(game, profile: str, mode: LinkMode,
                     log_fn=None) -> "IncrementalPlan | None":
    """Return an IncrementalPlan when this deploy can run incrementally.

    Every miss is logged with its reason and returns None (full path).
    Read-only — nothing on disk is touched here."""
    _log = _safe_log(log_fn)
    if not incremental_enabled():
        return None
    if not getattr(game, "supports_incremental_deploy", False):
        return None

    def _skip(reason: str) -> None:
        _log(f"Incremental deploy unavailable — {reason}; using the full path.")
        return None

    try:
        if game.get_last_deployed_profile() != profile:
            return _skip("a different profile was deployed last")
        if not game.get_deploy_active():
            return _skip("no active deployment")
        if game.get_last_deploy_mode() != mode.name:
            return _skip("deploy mode changed")

        deploy_dir = game.get_mod_data_path()
        if deploy_dir is None or not deploy_dir.is_dir():
            return _skip("no deploy directory")

        from Utils.deploy_standard import (
            _DEPLOY_MARKER_NAME, _DEPLOY_STATS_NAME, _load_deploy_stats)

        if not (deploy_dir / _DEPLOY_MARKER_NAME).is_file():
            return _skip("deploy marker missing")
        core_dir = _default_core(deploy_dir)
        if not core_dir.is_dir():
            return _skip(f"{core_dir.name}/ backup missing")

        state_dir = game.get_effective_filemap_path().parent
        stats_path = state_dir / _DEPLOY_STATS_NAME
        dfm_path = state_dir / DEPLOYED_FILEMAP_NAME
        if not stats_path.is_file() or not dfm_path.is_file():
            return _skip("previous deploy record missing")
        if (state_dir / "custom_deploy_log.txt").is_file():
            return _skip("leftover custom-deploy log present")

        # Per-separator overrides drive the wholesale-replace / dir-symlink /
        # per-mod link-mode machinery in the full path — v1 of the diff does
        # not mirror them, so any configured override falls back.
        from Utils.deploy_shared import load_separator_deploy_paths
        profile_dir = game.get_profile_root() / "profiles" / profile
        for info in (load_separator_deploy_paths(profile_dir) or {}).values():
            if not isinstance(info, dict):
                continue
            if info.get("path") or info.get("raw") or info.get("merge"):
                return _skip("separator deploy overrides configured")
            if (info.get("mode") or "").strip().lower() in ("hardlink", "symlink"):
                return _skip("separator link-mode overrides configured")

        old_filemap = load_deployed_filemap(dfm_path)
        if not old_filemap:
            return _skip("previous deploy record empty or unreadable")

        return IncrementalPlan(
            deploy_dir_str=str(deploy_dir),
            core_dir=core_dir,
            state_dir=state_dir,
            mode=mode,
            old_filemap=old_filemap,
            deploy_stats=_load_deploy_stats(stats_path),
        )
    except Exception as exc:                    # noqa: BLE001 — never block a deploy
        return _skip(f"eligibility check failed ({exc})")


# ---------------------------------------------------------------------------
# The diff-apply pass (called from deploy_filemap under an active plan)
# ---------------------------------------------------------------------------

def apply_incremental(
    plan: IncrementalPlan,
    tasks: list,
    rel_mod: "dict[str, tuple[str, str]]",
    *,
    deploy_dir: Path,
    core_dir: Path,
    overwrite_dir: Path,
    mode: LinkMode,
    state_dir: Path,
    staging_root: "Path | Mapping[str, Path] | None" = None,
    log_fn=None,
    progress_fn=None,
) -> "tuple[int, set[str]]":
    """Diff *tasks* (the new deployment) against plan.old_filemap and apply.

    tasks   — deploy_filemap's resolved task list:
              (src_str, dst_str, rel_lower, is_custom, use_symlink, override_mode)
    rel_mod — rel_lower -> (rel_str, mod_name) for the new deployment.

    Returns (files_linked, placed_lower) exactly like the full deploy_filemap
    path, so the calling handler code is none the wiser.  Raises
    IncrementalFallback on any anomaly.
    """
    _log = _safe_log(log_fn)
    from Utils.deploy_standard import (
        _DEPLOY_STATS_NAME, _MTIME_TOLERANCE_NS, _VANILLA_DEPLOYED_NAME,
        _load_vanilla_deployed, _write_deploy_stats, _write_vanilla_deployed)

    if any(t[3] for t in tasks):
        raise IncrementalFallback("custom-location tasks present")

    deploy_dir_str = str(deploy_dir)
    plen = len(deploy_dir_str) + 1
    old = plan.old_filemap
    stats = plan.deploy_stats

    new_tasks: dict[str, tuple] = {t[2]: t for t in tasks}
    new_rels = set(new_tasks)
    if not new_rels:
        raise IncrementalFallback("new deployment set is empty")

    def _eff_mode(t) -> LinkMode:
        return LinkMode.SYMLINK if t[4] else (t[5] if t[5] is not None else mode)

    # ---- read-only phase: classify --------------------------------------
    removed = [r for r in old if r not in new_rels]
    added = [r for r in new_rels if r not in old]
    moved = [r for r in new_rels
             if r in old and old[r][1] != rel_mod[r][1]]

    # Same-rel-same-mod entries: the staged file may have been replaced
    # (reinstall/update breaks hardlinks and stales copies).  Non-symlink
    # placements compare the *source* stat against the recorded deploy stat
    # (hardlinks share the inode, copy2 preserves mtime, so an untouched
    # source still matches).  Symlink placements just verify the link target.
    to_check = [new_tasks[r] for r in new_rels
                if r in old and old[r][1] == rel_mod[r][1]]
    changed: list[str] = []

    def _check_one(t) -> "str | None":
        src, dst, rel_lower, _ic, _us, _ov = t
        try:
            dstat = os.lstat(dst)
        except OSError:
            return rel_lower                # destination vanished — relink
        if _stat_m.S_ISLNK(dstat.st_mode):
            # Symlink placement (requested, or hardlink fell back to it):
            # intact iff it still points at the staging source.
            try:
                return None if os.readlink(dst) == src else rel_lower
            except OSError:
                return rel_lower
        try:
            sst = os.lstat(src)
        except OSError:
            return rel_lower                # source vanished — relink WARNs
        if dstat.st_ino == sst.st_ino and dstat.st_dev == sst.st_dev:
            return None                     # hardlink intact — airtight check
        # Copy placement (or a hardlink whose staging side was replaced,
        # breaking the link).  Unchanged only when BOTH sides still match the
        # recorded deploy-time stat: the destination check catches in-place
        # edits / runtime files overwriting a deployed path (mirrors the
        # restore rescue logic), the source check catches replaced staging
        # files.  copy2 preserves mtime, so an untouched pair matches; the
        # FAT/exFAT tolerance mirrors _MTIME_TOLERANCE_NS's purpose there.
        ds = stats.get(rel_lower)
        if ds is None:
            return rel_lower
        if dstat.st_size != ds[0] or abs(dstat.st_mtime_ns - ds[1]) > _MTIME_TOLERANCE_NS:
            return rel_lower
        if sst.st_size != ds[0] or abs(sst.st_mtime_ns - ds[1]) > _MTIME_TOLERANCE_NS:
            return rel_lower
        return None

    # Batched: at ~125k entries a per-item pool.map spends seconds on future
    # dispatch alone (the whole reason a no-change redeploy felt slow).
    for r in _map_batched(_check_one, to_check):
        if r is not None:
            changed.append(r)

    relink = set(added) | set(moved) | set(changed)
    delta = len(relink) + len(removed)
    scale = max(len(old), len(new_rels))
    if delta > _DELTA_FALLBACK_RATIO * scale:
        raise IncrementalFallback(
            f"too many changes for an incremental deploy ({delta} of {scale})")

    _log(f"  Incremental: {len(added)} added, {len(removed)} removed, "
         f"{len(moved)} moved, {len(changed)} refreshed, "
         f"{len(new_rels) - len(relink)} unchanged.")

    # ---- mutation phase ---------------------------------------------------
    dir_listing_cache: dict[str, dict[str, str]] = {}
    resolved_dir_cache: dict[str, str] = {}
    core_str = str(core_dir)

    _core_index_cache: "list[dict[str, tuple[str, str]]]" = []

    def _core_index() -> "dict[str, tuple[str, str]]":
        """rel_lower → (rel_str, path) map of the vanilla backup (lazy walk)."""
        if not _core_index_cache:
            idx: dict[str, tuple[str, str]] = {}
            cplen = len(core_str) + 1
            for dp, _dns, fns in os.walk(core_str):
                for fn in fns:
                    cp = dp + "/" + fn
                    rel = cp[cplen:]
                    idx[rel.lower()] = (rel, cp)
            _core_index_cache.append(idx)
        return _core_index_cache[0]

    def _core_lookup(rel_lower: str) -> "str | None":
        hit = _core_index().get(rel_lower)
        return hit[1] if hit is not None else None

    rescued_overwrite: list[str] = []
    rescued_to_mod = 0

    def _clear_dst(dst: str, rel_lower: str,
                   staging_dst: "str | None" = None) -> None:
        """Clear whatever sits at *dst* so a new link can land (or the path
        can stay vacant).  Managed placements — symlinks, hardlinks, copies
        still matching the deploy record or the vanilla backup — are
        discarded (staging/core owns the data); anything else is a runtime
        or externally-edited file.  When *staging_dst* is given (same-mod
        "changed" rels), an edited file is moved back onto its staging
        source — the restore path's xEdit semantics — so the relink deploys
        the edited content; otherwise it is rescued to overwrite/."""
        try:
            st = os.lstat(dst)
        except OSError:
            return
        if _stat_m.S_ISLNK(st.st_mode):
            os.unlink(dst)
            return
        if not _stat_m.S_ISREG(st.st_mode):
            raise IncrementalFallback(f"unexpected non-file entry at {dst}")
        if st.st_nlink > 1:
            os.unlink(dst)              # our hardlink (mod file or vanilla fill)
            return
        ds = stats.get(rel_lower)
        if ds is not None and st.st_size == ds[0] \
                and abs(st.st_mtime_ns - ds[1]) <= _MTIME_TOLERANCE_NS:
            os.unlink(dst)              # our copied mod file, unmodified
            return
        cp = _core_lookup(rel_lower)
        if cp is not None:
            try:
                cst = os.lstat(cp)
                if st.st_ino == cst.st_ino or (
                        st.st_size == cst.st_size
                        and abs(st.st_mtime_ns - cst.st_mtime_ns)
                        <= _MTIME_TOLERANCE_NS):
                    os.unlink(dst)      # vanilla gap-fill copy
                    return
            except OSError:
                pass
        rel_str = dst[plen:]
        if staging_dst is not None:
            nonlocal rescued_to_mod
            _move_crash_safe(dst, staging_dst)
            rescued_to_mod += 1
            mod_name = rel_mod[rel_lower][1]
            if mod_name != _OVERWRITE_NAME and staging_root is not None \
                    and rel_str.lower().endswith((".esp", ".esm", ".esl")):
                _mod_dir = resolve_mod_dir(staging_root, mod_name)
                if _mod_dir is not None:
                    from Utils.deploy_standard import _tag_mod_xedit_modified
                    _tag_mod_xedit_modified(_mod_dir, os.path.basename(rel_str))
            return
        _move_crash_safe(dst, str(overwrite_dir) + "/" + rel_str)
        rescued_overwrite.append(rel_str)

    # Removed rels: clear the deployed file, then refill the path from the
    # vanilla backup when the game shipped a file there.
    refill_tasks: list[tuple[str, str]] = []       # (core_src, dst)
    prune_dirs: set[str] = set()
    for rel_lower in removed:
        rel_str = old[rel_lower][0].replace("\\", "/")
        dst = _resolve_root_path_str(deploy_dir_str, rel_str,
                                     dir_listing_cache,
                                     resolved_dir_cache=resolved_dir_cache)
        _clear_dst(dst, rel_lower)
        prune_dirs.add(os.path.dirname(dst))
        cp = _core_lookup(rel_lower)
        if cp is not None:
            refill_tasks.append((cp, dst))

    # Relinked rels: clear whatever occupies the destination (stale vanilla
    # fill, old mod link, runtime file) before the new link lands.  For
    # same-mod "changed" rels an edited destination goes back onto its
    # staging source (xEdit semantics — the relink then redeploys the edit);
    # added/moved rels rescue foreign content to overwrite/.
    changed_set = set(changed)
    for rel_lower in relink:
        t = new_tasks[rel_lower]
        _clear_dst(t[1], rel_lower,
                   staging_dst=t[0] if rel_lower in changed_set else None)

    link_specs = [
        (new_tasks[r][0], new_tasks[r][1], r, _eff_mode(new_tasks[r]))
        for r in relink
    ]
    needed_dirs = {os.path.dirname(d) for _s, d, _r, _m in link_specs}
    needed_dirs.update(os.path.dirname(d) for _s, d in refill_tasks)
    _mkdir_leaves(needed_dirs)

    linked = 0
    done = 0
    total_ops = len(link_specs) + len(refill_tasks)
    placed_relinked: set[str] = set()
    stats_new: dict[str, str] = {}
    vanilla_added: list[str] = []

    def _do_one(spec):
        src, dst, rel_lower, em = spec
        actual, err = _do_link_ex(src, dst, em)
        if err is not None:
            return rel_lower, None, (dst, err), None
        line = None
        if actual is not LinkMode.SYMLINK:
            try:
                dstat = os.lstat(dst)
                if _stat_m.S_ISREG(dstat.st_mode):
                    line = (f"{dst[plen:]}\t{dstat.st_size}"
                            f"\t{dstat.st_mtime_ns}\n")
            except OSError:
                pass
        return rel_lower, actual, None, line

    if link_specs:
        with concurrent.futures.ThreadPoolExecutor(
                max_workers=_deploy_workers()) as pool:
            for rel_lower, actual, err, line in pool.map(_do_one, link_specs):
                done += 1
                if err is not None:
                    dst_err, exc = err
                    if getattr(exc, "errno", None) == errno.ENOSPC:
                        pool.shutdown(wait=True, cancel_futures=True)
                        _log(f"  ERROR: game drive is full — aborting deploy "
                             f"(failed at {dst_err}). Free up space, then run "
                             f"Restore and deploy again.")
                        raise OSError(errno.ENOSPC,
                                      f"Game drive full while deploying {dst_err}")
                    _log(f"  WARN: could not transfer {dst_err}: {exc}")
                    continue
                placed_relinked.add(rel_lower)
                linked += 1
                if line is not None:
                    stats_new[rel_lower] = line
                if progress_fn is not None and (done % 200 == 0 or done == total_ops):
                    progress_fn(done, total_ops)

    def _do_refill(item):
        cp, dst = item
        actual, err = _do_link_ex(cp, dst, mode)
        return dst, actual, err

    if refill_tasks:
        with concurrent.futures.ThreadPoolExecutor(
                max_workers=_deploy_workers()) as pool:
            for dst, actual, err in pool.map(_do_refill, refill_tasks):
                done += 1
                if err is not None:
                    if getattr(err, "errno", None) == errno.ENOSPC:
                        pool.shutdown(wait=True, cancel_futures=True)
                        raise OSError(errno.ENOSPC,
                                      f"Game drive full while deploying {dst}")
                    _log(f"  WARN: could not restore vanilla {dst}: {err}")
                    continue
                if actual is LinkMode.SYMLINK:
                    vanilla_added.append(dst[plen:])
                if progress_fn is not None and (done % 200 == 0 or done == total_ops):
                    progress_fn(done, total_ops)

    # Prune directories emptied by the removals (deepest first; rmdir only
    # succeeds on empty dirs, so refilled/live paths are naturally kept).
    for d in sorted(prune_dirs, key=lambda p: p.count("/"), reverse=True):
        cur = d
        while cur != deploy_dir_str and cur.startswith(deploy_dir_str + "/"):
            try:
                os.rmdir(cur)
            except OSError:
                break
            cur = os.path.dirname(cur)

    if rescued_to_mod:
        _log(f"  Rescued {rescued_to_mod} edited file(s) back to mod folder(s).")
    if rescued_overwrite:
        _log(f"  Rescued {len(rescued_overwrite)} runtime/edited file(s) → overwrite/.")
        _append_overwrite_log(overwrite_dir, rescued_overwrite, _log)
        _record_overwrite_index(overwrite_dir, rescued_overwrite, _log)

    # ---- refresh the on-disk records ---------------------------------------
    final_placed = (new_rels - relink) | placed_relinked

    stats_lines: list[str] = []
    for rel_lower in new_tasks:
        if rel_lower not in final_placed:
            continue
        if rel_lower in placed_relinked:
            line = stats_new.get(rel_lower)
            if line is not None:
                stats_lines.append(line)
        else:
            ds = stats.get(rel_lower)
            if ds is not None:
                # Kept file — previous record still accurate.  The loader
                # lowercases rels, so the new filemap's casing is fine here.
                stats_lines.append(f"{rel_mod[rel_lower][0]}\t{ds[0]}\t{ds[1]}\n")
    _write_deploy_stats(state_dir / _DEPLOY_STATS_NAME, stats_lines,
                        log_fn=log_fn)

    write_deployed_filemap(
        state_dir / DEPLOYED_FILEMAP_NAME,
        [(rel_mod[r][0], rel_mod[r][1]) for r in new_tasks if r in final_placed],
        log_fn=log_fn)

    # Vanilla symlink manifest: rels that became mod-covered leave the set,
    # symlink-mode refills join it.
    manifest_path = state_dir / _VANILLA_DEPLOYED_NAME
    vanilla = _load_vanilla_deployed(manifest_path)
    dropped = vanilla & relink
    new_vanilla = [v for v in vanilla_added if v.lower() not in vanilla]
    if dropped or new_vanilla:
        vanilla -= dropped
        _write_vanilla_deployed(
            manifest_path, sorted(vanilla) + new_vanilla, log_fn=log_fn)

    if verify_enabled():
        _verify(new_tasks, final_placed, _eff_mode, _core_index(),
                deploy_dir_str, dir_listing_cache, resolved_dir_cache, _log)

    plan.ran_incremental = True
    return linked, final_placed


def _record_overwrite_index(overwrite_dir: Path, rels: "list[str]", _log) -> None:
    """Append rescued rels to modindex.bin under [Overwrite] (mirror of the
    restore_data_core bookkeeping) so the next filemap build sees them."""
    try:
        from Utils.filemap import read_mod_index, update_mod_index
        index_path = overwrite_dir.parent / "modindex.bin"
        existing = read_mod_index(index_path) or {}
        existing_normal, existing_root = existing.get(_OVERWRITE_NAME, ({}, {}))
        new_normal: dict[str, str] = dict(existing_normal)
        for rel_str in rels:
            rel_posix = rel_str.replace("\\", "/")
            new_normal[rel_posix.lower()] = rel_posix
        update_mod_index(index_path, _OVERWRITE_NAME, new_normal, existing_root)
    except Exception:
        pass


def _verify(new_tasks: dict, final_placed: "set[str]", eff_mode_fn,
            core_index: "dict[str, tuple[str, str]]", deploy_dir_str: str,
            dir_listing_cache: dict, resolved_dir_cache: dict, _log) -> None:
    """AMM_DEPLOY_VERIFY=1 — check every placed rel is correctly linked and
    every uncovered vanilla file is present.  Logs a summary, never raises.
    A debugging aid: clarity over speed."""
    from Utils.deploy_standard import _MTIME_TOLERANCE_NS

    mismatches = 0

    def _check(rel_lower: str) -> int:
        t = new_tasks[rel_lower]
        src, dst = t[0], t[1]
        em = eff_mode_fn(t)
        try:
            st = os.lstat(dst)
        except OSError:
            return 1
        if em is LinkMode.SYMLINK:
            try:
                return 0 if os.readlink(dst) == src else 1
            except OSError:
                return 1
        try:
            sst = os.lstat(src)
        except OSError:
            return 1
        if st.st_ino == sst.st_ino and st.st_dev == sst.st_dev:
            return 0
        # copy / fell back to copy — size + mtime parity
        if st.st_size == sst.st_size \
                and abs(st.st_mtime_ns - sst.st_mtime_ns) <= _MTIME_TOLERANCE_NS:
            return 0
        return 1

    for r in _map_batched(_check, list(final_placed)):
        mismatches += r

    # Vanilla coverage: every core rel not shadowed by a mod must exist at
    # its (case-resolved) deploy path.
    missing_vanilla = 0
    for rel_lower, (rel_str, _cp) in core_index.items():
        if rel_lower in final_placed:
            continue
        dst = _resolve_root_path_str(deploy_dir_str, rel_str.replace("\\", "/"),
                                     dir_listing_cache,
                                     resolved_dir_cache=resolved_dir_cache)
        if not os.path.lexists(dst):
            missing_vanilla += 1

    if mismatches or missing_vanilla:
        _log(f"  VERIFY: {mismatches} mismatched deployed file(s), "
             f"{missing_vanilla} missing vanilla file(s) — incremental deploy "
             f"diverged; run a full deploy (AMM_DEPLOY_INCREMENTAL=0) and report.")
    else:
        _log("  VERIFY: incremental deploy matches expectation.")


__all__ = [
    "DEPLOYED_FILEMAP_NAME",
    "IncrementalFallback",
    "IncrementalPlan",
    "active_for",
    "activate",
    "apply_incremental",
    "deactivate",
    "incremental_enabled",
    "is_active",
    "load_deployed_filemap",
    "plan_incremental",
    "verify_enabled",
    "write_deployed_filemap",
]
