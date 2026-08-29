"""Single Python service boundary for catalog, resolver, and deploy consumers."""

from __future__ import annotations

import os
import shutil
import tempfile
import threading
import time
import uuid
import weakref
from pathlib import Path
from typing import Callable, Iterable, Iterator

from Utils.filegraph_adapter import (
    FLAG_ARCHIVE, FLAG_FRAMEWORK, FLAG_PLUGIN, FLAG_PRE_RTX, FLAG_ROOT_RULE,
    FLAG_TEXT,
    OVERWRITE_NAME, ROOT_FOLDER_NAME, GameCandidateAdapter,
)
from Utils.filegraph_models import (
    AssetCopy, CatalogStatus, ConflictState, ConflictSummary, DeployedStateEntry,
    DeployEntry, DeploymentPlan, FileGraphBusy, FileGraphCancelled,
    FileGraphRecoveryRequired, FileGraphStale, ModFile, OperationRecord,
    ResolutionDelta, SnapshotExport, Winner,
)
from Utils.filegraph_native import pack, require_native, unpack


def _native_error(exc: BaseException) -> BaseException:
    text = str(exc)
    lowered = text.lower()
    if ("deployment" in lowered
            and ("still active" in lowered or "is active" in lowered)):
        return FileGraphRecoveryRequired(
            "A previous deployment was interrupted. Run Deploy or Restore to "
            "recover its filesystem journal before changing this profile."
        )
    if "writer lock" in lowered or "busy" in lowered:
        return FileGraphBusy(
            "This mod library is being changed by another Amethyst process. "
            "Close the other process or wait for its Refresh/deployment to finish."
        )
    if "cancelled" in text.lower():
        return FileGraphCancelled(text)
    return exc


class CancellationToken:
    """Thread-safe token understood by both Python scans and native work."""

    __slots__ = ("_native",)

    def __init__(self):
        self._native = require_native().CancelToken()

    def cancel(self) -> None:
        self._native.cancel()

    def reset(self) -> None:
        self._native.reset()

    def is_cancelled(self) -> bool:
        return bool(self._native.is_cancelled())


class ResolvedSnapshot:
    """Immutable generation-pinned snapshot; safe across later reconciles."""

    __slots__ = (
        "_native", "_export", "_asset_winners_cache", "_asset_sources_cache",
    )

    def __init__(self, native_snapshot):
        self._native = native_snapshot
        self._export: SnapshotExport | None = None
        self._asset_winners_cache: dict[tuple[str, ...], tuple[Winner, ...]] = {}
        self._asset_sources_cache: dict[
            tuple[str, ...], tuple[AssetCopy, ...]
        ] = {}

    @property
    def generation(self) -> int:
        return int(self._native.generation)

    @property
    def inventory_generation(self) -> int:
        return int(self._native.inventory_generation)

    @property
    def loose_beats_archive(self) -> bool:
        return bool(self._native.loose_beats_archive)

    def export(self) -> SnapshotExport:
        cached = self._export
        if cached is None:
            cached = SnapshotExport.from_wire(unpack(self._native.export()))
            self._export = cached
        return cached

    def conflict_state(self) -> ConflictState:
        return ConflictState.from_wire(unpack(self._native.conflict_state()))

    def framework_winners(self) -> tuple[Winner, ...]:
        return tuple(
            Winner.from_wire(value)
            for value in unpack(self._native.framework_winners())
        )

    def flagged_winners(self, flags: int) -> tuple[Winner, ...]:
        return tuple(
            Winner.from_wire(value)
            for value in unpack(self._native.flagged_winners(int(flags)))
        )

    def staged_plugins(self) -> set[str]:
        return set(unpack(self._native.staged_plugins()))

    def plugin_winners(self) -> dict[str, Winner]:
        return {
            name: Winner.from_wire(value)
            for name, value in unpack(self._native.plugin_winners()).items()
        }

    def has_deployed_path(self, path: str | bytes, *, basename: bool = False) -> bool:
        encoded = (path if isinstance(path, bytes) else path.replace(
            "\\", "/").lower().encode("utf-8", "surrogateescape"))
        return bool(self._native.has_deployed_path(encoded, basename))

    def patch_files(self) -> tuple[tuple[str, bytes], ...]:
        return tuple(
            (str(mod_name), bytes(relative))
            for mod_name, relative in unpack(self._native.patch_files())
        )

    def mod_files(self, mod_name: str) -> tuple[ModFile, ...]:
        return tuple(
            ModFile.from_wire(value)
            for value in unpack(self._native.mod_files(mod_name))
        )

    def mod_plugins(self, mod_name: str) -> tuple[str, ...]:
        """Compact plugin-name query used by toggle activation sync."""
        return tuple(map(str, unpack(self._native.mod_plugins(mod_name))))

    def archive_files(self, mod_name: str) -> tuple[ModFile, ...]:
        return tuple(
            ModFile.from_wire(value)
            for value in unpack(self._native.archive_files(mod_name))
        )

    def inventory_facets(self) -> dict:
        return unpack(self._native.inventory_facets())

    def raw_files_by_basename(
        self, basenames: Iterable[str],
    ) -> tuple[tuple[str, bytes], ...]:
        return tuple(
            (str(mod_name), bytes(relative))
            for mod_name, relative in unpack(
                self._native.raw_files_by_basename(list(basenames)))
        )

    def winner_by_suffix(self, suffix: str | bytes) -> Winner | None:
        encoded = (suffix if isinstance(suffix, bytes) else suffix.replace(
            "\\", "/").lower().encode("utf-8", "surrogateescape"))
        value = unpack(self._native.winner_by_suffix(encoded))
        return Winner.from_wire(value) if value else None

    def asset_winners(self, prefixes: str | Iterable[str] = ()) -> tuple[Winner, ...]:
        values = ((prefixes,) if isinstance(prefixes, str)
                  else tuple(prefixes))
        key = tuple(str(value).replace("\\", "/").lower() for value in values)
        cached = self._asset_winners_cache.get(key)
        if cached is not None:
            return cached
        cached = tuple(
            Winner.from_wire(value)
            for value in unpack(self._native.asset_winners(list(key)))
        )
        self._asset_winners_cache[key] = cached
        return cached

    def asset_copies(
        self, mod_names: Iterable[str], *, prefixes: Iterable[str] = (),
        exact_paths: Iterable[str] = (), extensions: Iterable[str] = (),
    ) -> tuple[AssetCopy, ...]:
        return tuple(
            AssetCopy.from_wire(value)
            for value in unpack(self._native.asset_copies(
                set(mod_names), list(prefixes), list(exact_paths),
                list(extensions)))
        )

    def asset_winner_sources(
        self, prefixes: str | Iterable[str] = (),
    ) -> tuple[AssetCopy, ...]:
        values = ((prefixes,) if isinstance(prefixes, str)
                  else tuple(prefixes))
        key = tuple(str(value).replace("\\", "/").lower() for value in values)
        cached = self._asset_sources_cache.get(key)
        if cached is None:
            cached = tuple(
                AssetCopy.from_wire(value)
                for value in unpack(
                    self._native.asset_winner_sources(list(key)))
            )
            self._asset_sources_cache[key] = cached
        return cached

    def framework_basenames(self, mod_names: Iterable[str]) -> set[str]:
        return set(unpack(self._native.framework_basenames(set(mod_names))))

    def deployment_plan(self) -> DeploymentPlan:
        return DeploymentPlan.from_wire(unpack(self._native.deployment_plan()))

    def data_entries(self) -> tuple[tuple[int, str, str, str, bool], ...]:
        """Compact Data-tab projection: id, owner, target, spelling, conflict."""
        return tuple(
            (int(value[0]), str(value[1]), str(value[2]), str(value[3]),
             bool(value[4]))
            for value in unpack(self._native.data_entries())
        )

    def deployment_entries(self, candidate_ids: Iterable[int]) -> tuple[DeployEntry, ...]:
        return tuple(
            DeployEntry.from_wire(value)
            for value in unpack(self._native.deployment_entries(
                {int(candidate_id) for candidate_id in candidate_ids}))
        )

    def contested_winner_ids(self, candidate_ids: Iterable[int]) -> set[int]:
        return {
            int(candidate_id)
            for candidate_id in unpack(self._native.contested_winner_ids(
                {int(candidate_id) for candidate_id in candidate_ids}))
        }

    def contested_paths(self) -> set[tuple[str, bytes]]:
        return {
            (target, bytes(path))
            for target, path in unpack(self._native.contested_paths())
        }

    def winner(self, target: str, path: str | bytes,
               namespace: str = "normal") -> Winner | None:
        encoded = path if isinstance(path, bytes) else path.lower().encode(
            "utf-8", "surrogateescape")
        value = unpack(self._native.winner(namespace, target, encoded))
        return Winner.from_wire(value) if value else None

    def providers(self, target: str, path: str | bytes,
                  kinds: Iterable[str] = (), namespace: str = "normal"):
        encoded = path if isinstance(path, bytes) else path.lower().encode(
            "utf-8", "surrogateescape")
        allowed = set(kinds)
        values = unpack(self._native.providers(namespace, target, encoded))
        if allowed:
            values = [value for value in values if value["kind"] in allowed]
        from Utils.filegraph_models import Provider
        return tuple(Provider.from_wire(value) for value in values)

    def conflict_summary(self, mod_name: str) -> ConflictSummary:
        return ConflictSummary.from_wire(
            unpack(self._native.conflict_summary(mod_name)))

    def conflict_partners(self, mod_name: str,
                          kinds: Iterable[str] = ()) -> set[str]:
        return set(unpack(self._native.conflict_partners(
            mod_name, list(kinds))))

    def conflict_files(self, first: str, second: str,
                       kinds: Iterable[str] = ()) -> list[tuple[str, str]]:
        rows = unpack(self._native.conflict_files(first, second, list(kinds)))
        return [
            (target, bytes(path).decode("utf-8", "surrogateescape"))
            for target, path in rows
        ]

    def archive_member_conflicts(
        self, mod_name: str, source_rel: str | bytes,
    ) -> dict[str, int]:
        encoded = (source_rel if isinstance(source_rel, bytes) else
                   source_rel.encode("utf-8", "surrogateescape"))
        return {
            str(path): int(status)
            for path, status in unpack(
                self._native.archive_member_conflicts(mod_name, encoded))
        }

    def iter_winners(
        self, *, target: str | None = None, namespaces: Iterable[str] = (),
        after_id: int = 0, limit: int | None = None,
    ) -> Iterator[Winner]:
        cursor = int(after_id)
        remaining = None if limit is None else max(0, int(limit))
        allowed = list(namespaces)
        while remaining is None or remaining:
            page_size = 1000 if remaining is None else min(1000, remaining)
            values = unpack(self._native.iter_winners(
                target, allowed, cursor, page_size))
            if not values:
                break
            for value in values:
                winner = Winner.from_wire(value)
                cursor = max(cursor, winner.candidate_id)
                yield winner
            if remaining is not None:
                remaining -= len(values)
            if len(values) < page_size:
                break

    def iter_mod_files(
        self, mod_name: str, *, winners_only: bool = False,
        kinds: Iterable[str] = (), cursor: int = 0,
        limit: int | None = None,
    ) -> Iterator[ModFile]:
        cursor = max(0, int(cursor))
        remaining = None if limit is None else max(0, int(limit))
        allowed = list(kinds)
        while remaining is None or remaining:
            page_size = 1000 if remaining is None else min(1000, remaining)
            values = unpack(self._native.iter_mod_files(
                mod_name, bool(winners_only), allowed, cursor, page_size))
            if not values:
                break
            for value in values:
                candidate = ModFile.from_wire(value)
                yield candidate
            cursor += len(values)
            if remaining is not None:
                remaining -= len(values)
            if len(values) < page_size:
                break

    def plugin_owner(self, plugin: str) -> str | None:
        winner = self.plugin_winners().get(plugin.lower())
        return winner.mod_name if winner is not None else None

    def framework_statuses(self):
        # Framework definitions remain game-specific Python policy; the service
        # consumer evaluates them against iter_winners without reading maps.
        return ()

    def changes_since(self, generation: int):
        if generation == self.generation:
            return ()
        return self.export().winners


class ProfileSession:
    __slots__ = (
        "library", "adapter", "_native", "_snapshot", "_lock",
        "_archive_inventory_generation", "_archive_selection",
        "_archive_records",
        "_pending_deployment_plans", "_committed_deployment_plan",
        "_committed_deployment_mode", "_deployment_matches_committed",
        "_deployment_match_known", "_deployment_projection_cache",
        "_prepared_deployment_plan", "_deployment_prepare_lock",
        "_intent_identity", "_deployed_entries_cache",
    )

    def __init__(self, library: "LibrarySession", profile_dir: Path):
        self.library = library
        self.adapter = GameCandidateAdapter(
            library.game, profile_dir, log_fn=library.log,
            staging_dir=library.root / "mods")
        profile_id = str(profile_dir.resolve(strict=False))
        self._native = library._native.open_profile(profile_id)
        self._snapshot: ResolvedSnapshot | None = None
        self._lock = threading.RLock()
        self._archive_inventory_generation = -1
        self._archive_selection: tuple[tuple[str, str], ...] = ()
        self._archive_records: tuple[tuple, ...] = ()
        self._pending_deployment_plans: dict[str, tuple[DeploymentPlan, str]] = {}
        self._committed_deployment_plan: DeploymentPlan | None = None
        self._committed_deployment_mode: str | None = None
        self._deployment_matches_committed = False
        self._deployment_match_known = False
        self._deployment_projection_cache: dict[
            str, tuple[object, object]
        ] = {}
        self._prepared_deployment_plan: DeploymentPlan | None = None
        # Serialize deployment-plan expansion with deployment journalling.
        self._deployment_prepare_lock = threading.RLock()
        self._intent_identity: bytes | None = None
        # Restoring 100k+ deployed rows from SQLite/MessagePack is cached after
        # the first incremental-deploy probe. A successful commit or targeted
        # forget drops it because those operations change authoritative rows.
        self._deployed_entries_cache: (
            tuple[DeployedStateEntry, ...] | None
        ) = None

    def _rebind_game(self, game, log_fn) -> None:
        """Replace game-derived policy after the handler object is reloaded.

        Custom-game edits and handler updates rebuild the game registry while
        keeping the native library warm.  The profile adapter owns the routing
        projection, so leaving it attached to the previous object would keep
        deriving route variants from stale custom rules.
        """
        if self.adapter.game is game:
            self.adapter.log = log_fn
            return
        replacement = GameCandidateAdapter(
            game, self.profile_dir, log_fn=log_fn,
            staging_dir=self.library.root / "mods")
        with self._lock:
            self.adapter = replacement
            self._archive_inventory_generation = -1
            self._archive_selection = ()
            self._archive_records = ()
            self._invalidate_resolution_cache()

    @property
    def profile_dir(self) -> Path:
        return self.adapter.profile_dir

    def reconcile(
        self,
        intent: dict | None = None,
        operation_hint: dict | None = None,
        cancel: CancellationToken | None = None,
        timing=None,
    ) -> ResolutionDelta:
        token = cancel or CancellationToken()
        phase_started = time.perf_counter()
        if intent is None:
            intent = self._catalog_backed_intent(operation_hint, token)
        else:
            intent = dict(intent)
            if operation_hint is not None:
                intent["hint"] = operation_hint
        if timing is not None:
            timing.mark("catalog-backed profile intent prepared",
                        phase_started=phase_started, lane="worker")
        lock_started = time.perf_counter()
        with self._lock:
            if timing is not None:
                timing.mark("profile-session reconcile lock acquired",
                            phase_started=lock_started, lane="worker")
            try:
                phase_started = time.perf_counter()
                encoded = pack(intent)
                if timing is not None:
                    timing.mark(
                        f"profile intent encoded ({len(encoded)} bytes)",
                        phase_started=phase_started, lane="worker")
                phase_started = time.perf_counter()
                raw = self._native.reconcile(encoded, token._native)
                if timing is not None:
                    timing.mark(
                        f"native reconcile returned ({len(raw)} bytes)",
                        phase_started=phase_started, lane="worker")
            except BaseException as exc:
                if timing is not None:
                    timing.finish(
                        f"native reconcile failed: {exc}", lane="worker")
                raise _native_error(exc) from exc
            phase_started = time.perf_counter()
            delta = ResolutionDelta.from_wire(unpack(raw))
            self._snapshot = ResolvedSnapshot(self._native.snapshot())
            self._intent_identity = self._semantic_intent_identity(intent)
            # Retain at most the previous generation's disposable deployment
            # plan until Deploy replaces it. Every read validates
            # generation (and projection caches validate plan identity), so it
            # cannot be consumed after this reconcile.  Clearing it here made
            # the conflict worker synchronously decref tens of thousands of
            # DeployEntry objects, adding a visible pause to small toggles.
            if timing is not None:
                changed_winners = (
                    len(delta.changed_winner_ids)
                    + len(delta.removed_winner_ids))
                timing.mark(
                    "native delta decoded and snapshot pinned "
                    f"(gen={delta.base_generation}->{delta.generation}, "
                    f"full={delta.full_rebuild}, "
                    f"candidates={delta.candidates_touched}, "
                    f"destinations={delta.destinations_touched}, "
                    f"winners={changed_winners}, "
                    f"edges={len(delta.changed_edges)}, "
                    f"summaries={len(delta.changed_summaries)}, "
                    f"graph={delta.graph_compute_ns / 1e6:.3f}ms, "
                    f"sqlite={delta.sqlite_commit_ns / 1e6:.3f}ms)",
                    phase_started=phase_started, lane="worker")
            if delta.deployment_dirty:
                self._deployment_matches_committed = False
                # A native winner delta is definitive: no deployed-state scan
                # is needed merely to rediscover that this generation differs.
                self._deployment_match_known = True
            return delta

    @staticmethod
    def _semantic_intent_identity(intent: dict) -> bytes:
        """Stable intent payload excluding the resolver operation hint."""
        semantic = dict(intent)
        semantic.pop("hint", None)
        return pack(semantic)

    def _invalidate_resolution_cache(self) -> None:
        """Drop disposable state after a catalog inventory replacement."""
        with self._lock:
            stale_plan = self._prepared_deployment_plan
            self._snapshot = None
            self._intent_identity = None
            self._prepared_deployment_plan = None
            if stale_plan is not None:
                for cache_key, cached in list(
                        self._deployment_projection_cache.items()):
                    if cached[0] is stale_plan:
                        self._deployment_projection_cache.pop(cache_key, None)

    def _reset_after_catalog_rebuild(self, profile_id: str) -> None:
        with self._deployment_prepare_lock:
            with self._lock:
                self._native = self.library._native.open_profile(profile_id)
                self._snapshot = None
                self._intent_identity = None
                self._archive_inventory_generation = -1
                self._archive_selection = ()
                self._archive_records = ()
                self._pending_deployment_plans.clear()
                self._committed_deployment_plan = None
                self._committed_deployment_mode = None
                self._deployment_matches_committed = False
                self._deployment_match_known = False
                self._deployment_projection_cache.clear()
                self._prepared_deployment_plan = None
                self._deployed_entries_cache = None

    def ensure_reconciled(
        self,
        operation_hint: dict | None = None,
        cancel: CancellationToken | None = None,
        timing=None,
    ) -> bool:
        """Reconcile only when authoritative intent or inventory changed.

        Deployment used to create a fresh graph generation unconditionally.
        Besides redundant graph/SQLite work, that invalidated the exact plan
        warmed after the last UI edit. Return ``False`` when the pinned
        generation already represents the current files and rules.
        """
        token = cancel or CancellationToken()
        intent = self._catalog_backed_intent(operation_hint, token)
        identity = self._semantic_intent_identity(intent)
        inventory_generation = self.library.status().inventory_generation
        with self._lock:
            snapshot = self.snapshot()
            current = (
                snapshot.generation > 0
                and snapshot.inventory_generation == inventory_generation
                and self._intent_identity == identity
            )
        if current:
            return False
        self.reconcile(intent=intent, operation_hint=operation_hint,
                       cancel=token, timing=timing)
        return True

    def _catalog_backed_intent(
        self, operation_hint: dict | None, cancel: CancellationToken,
    ) -> dict:
        """Ensure rule variants and archive ranks exist without a disk scan."""
        with self.library._refresh_lock:
            hint_kind = str((operation_hint or {}).get("kind", "full"))
            self.adapter.prepare_profile_rules(
                refresh_rules=hint_kind not in {
                    "toggle", "enable", "disable",
                })
            intent = self.adapter.build_intent(operation_hint)
            variants = self.library.variant_keys()
            wanted = [
                (entry["name"], entry["key"], entry["variant_key"])
                for entry in intent["mods"]
            ]
            for special in (OVERWRITE_NAME, ROOT_FOLDER_NAME):
                wanted.append((
                    special, special.lower(), self.adapter.variant_key(special)))
            for mod_name, mod_key, variant_key in wanted:
                if variant_key in variants.get(mod_key, frozenset()):
                    continue
                if cancel.is_cancelled():
                    raise FileGraphCancelled("filegraph variant derivation cancelled")
                catalog_manifest = self.library.manifest_for_rederive(mod_name)
                if catalog_manifest is None:
                    self.library.log(
                        f"Catalog has no raw manifest for {mod_name}; run Refresh "
                        "after externally adding or changing mod folders."
                    )
                    continue
                batch = self.adapter.build_manifest(
                    mod_name, cancel=cancel,
                    catalog_manifest=catalog_manifest)
                self.library.replace_mod_manifest(batch, cancel=cancel)
                variants.setdefault(mod_key, frozenset())
                variants[mod_key] = variants[mod_key] | {variant_key}
            status = self.library.status()
            archive_selection = tuple(
                (mod_key, variant_key)
                for _mod_name, mod_key, variant_key in wanted
            )
            if (self._archive_inventory_generation != status.inventory_generation
                    or self._archive_selection != archive_selection):
                self._archive_records = self.library.archive_units(
                    archive_selection)
                self._archive_selection = archive_selection
                self._archive_inventory_generation = status.inventory_generation
            self.adapter.load_catalog_archive_order(self._archive_records)
            # Archive ranking is derived from the exact profile variants and
            # becomes part of authoritative intent. Rebuild the small intent
            # payload after the selected archive projection is loaded.
            intent = self.adapter.build_intent(operation_hint)
            return intent

    def snapshot(self) -> ResolvedSnapshot:
        with self._lock:
            if self._snapshot is None:
                self._snapshot = ResolvedSnapshot(self._native.snapshot())
            return self._snapshot

    def build_deployment_plan(self, snapshot_generation: int) -> DeploymentPlan:
        generation = int(snapshot_generation)
        with self._lock:
            cached = self._prepared_deployment_plan
            if cached is not None and cached.generation == generation:
                return cached

        with self._deployment_prepare_lock:
            with self._lock:
                cached = self._prepared_deployment_plan
                if cached is not None and cached.generation == generation:
                    return cached
                current = self.snapshot().generation
            if current != generation:
                raise FileGraphStale(
                    f"snapshot generation {generation} is stale; "
                    f"current generation is {current}")
            try:
                # Native construction + wire encoding releases the GIL. Do not
                # hold the profile reconcile lock while it runs: an external
                # edit may supersede it and the generation check below rejects
                # the stale plan.
                value = unpack(
                    self._native.build_deployment_plan(generation))
            except BaseException as exc:
                raise _native_error(exc) from exc
            plan = self._deployment_plan_from_wire(value)
            with self._lock:
                current = self.snapshot().generation
                if current != generation:
                    raise FileGraphStale(
                        f"prepared deployment generation {generation} was "
                        f"superseded by generation {current}")
                previous_plan = self._prepared_deployment_plan
                self._prepared_deployment_plan = plan
                if previous_plan is not None and previous_plan is not plan:
                    for cache_key, cached_projection in list(
                            self._deployment_projection_cache.items()):
                        if cached_projection[0] is previous_plan:
                            self._deployment_projection_cache.pop(
                                cache_key, None)
            return plan

    def _deployment_plan_from_wire(self, value) -> DeploymentPlan:
        roots = {
            OVERWRITE_NAME: self.adapter.overwrite,
            # Adapter paths are pinned to this profile. Consulting the shared
            # mutable game object's active profile from an idle worker could
            # attach another profile's Root_Folder after a fast switch.
            "[Root_Folder]": self.adapter.root_folder,
        }

        def source_root(mod_name: str):
            root = roots.get(mod_name)
            if root is None:
                root = self.adapter.staging / mod_name
                roots[mod_name] = root
            return root

        return DeploymentPlan.from_wire(
            value, source_root_for_mod=source_root)

    def begin_deployment(
        self,
        snapshot_generation: int,
        link_mode: str,
        transaction_id: str | None = None,
    ) -> tuple[str, DeploymentPlan]:
        transaction_id = transaction_id or str(uuid.uuid4())
        generation = int(snapshot_generation)
        # Expand the immutable plan here, after Deploy was requested. A cached
        # same-generation plan is still reused when another deployment-stage
        # consumer already requested it.
        with self._deployment_prepare_lock:
            plan = self.build_deployment_plan(generation)
            with self._lock:
                try:
                    if self.snapshot().generation != generation:
                        raise FileGraphStale(
                            f"deployment generation {generation} is stale")
                    self._native.begin_prepared_deployment(
                        transaction_id, generation, str(link_mode))
                except FileGraphStale:
                    raise
                except BaseException as exc:
                    raise _native_error(exc) from exc
                self._pending_deployment_plans[transaction_id] = (
                    plan, str(link_mode).lower())
        return transaction_id, plan

    def cached_deployment_plan(
        self, link_mode: str | None = None,
    ) -> DeploymentPlan | None:
        """Last plan committed through this profile session, if any."""
        if self._committed_deployment_plan is None:
            return None
        if (link_mode is not None
                and self._committed_deployment_mode != str(link_mode).lower()):
            return None
        return self._committed_deployment_plan

    def cached_deployment_projection(
        self, cache_key: str, source: object,
    ):
        """Return a consumer projection only for the identical source plan."""
        cached = self._deployment_projection_cache.get(str(cache_key))
        if cached is None or cached[0] is not source:
            return None
        return cached[1]

    def cache_deployment_projection(
        self, cache_key: str, source: object, projection: object,
    ) -> None:
        """Retain a warm projection without exposing it as service state."""
        self._deployment_projection_cache[str(cache_key)] = (
            source, projection)

    def deployment_unchanged(
        self, snapshot_generation: int, link_mode: str,
    ) -> bool:
        normalized_mode = str(link_mode).lower()
        if (self._deployment_match_known
                and self.snapshot().generation == int(snapshot_generation)):
            return bool(
                self._deployment_matches_committed
                and self._committed_deployment_mode == normalized_mode
            )
        with self._lock:
            try:
                matches = bool(self._native.deployment_unchanged(
                    int(snapshot_generation), normalized_mode))
            except BaseException as exc:
                raise _native_error(exc) from exc
        if matches:
            self._deployment_matches_committed = True
            self._deployment_match_known = True
            self._committed_deployment_mode = normalized_mode
        return matches

    def commit_deployment(self, transaction_id: str) -> None:
        with self._lock:
            try:
                self._native.commit_deployment(transaction_id)
            except BaseException as exc:
                raise _native_error(exc) from exc
            committed = self._pending_deployment_plans.pop(
                transaction_id, None)
            self._deployed_entries_cache = None
            if committed is not None:
                (self._committed_deployment_plan,
                 self._committed_deployment_mode) = committed
                self._deployment_matches_committed = True
                self._deployment_match_known = True

    def update_deployment_phase(
        self, transaction_id: str, phase: str,
    ) -> None:
        with self._lock:
            try:
                self._native.update_deployment_phase(transaction_id, phase)
            except BaseException as exc:
                raise _native_error(exc) from exc

    def fail_deployment(self, transaction_id: str) -> None:
        with self._lock:
            try:
                self._native.fail_deployment(transaction_id)
            except BaseException as exc:
                raise _native_error(exc) from exc
            self._pending_deployment_plans.pop(transaction_id, None)

    def incomplete_operations(self) -> tuple[OperationRecord, ...]:
        with self._lock:
            try:
                rows = unpack(self._native.incomplete_operations())
            except BaseException as exc:
                raise _native_error(exc) from exc
        return tuple(OperationRecord.from_wire(row) for row in rows)

    def deployed_entries(self) -> tuple[DeployedStateEntry, ...]:
        with self._lock:
            if self._deployed_entries_cache is not None:
                return self._deployed_entries_cache
            try:
                rows = unpack(self._native.deployed_entries())
            except BaseException as exc:
                raise _native_error(exc) from exc
            entries = tuple(
                DeployedStateEntry.from_wire(row) for row in rows)
            self._deployed_entries_cache = entries
            return entries

    def forget_deployed_mods(self, mod_names: Iterable[str]) -> int:
        with self._lock:
            try:
                removed = int(self._native.forget_deployed_mods(
                    [name.lower() for name in mod_names]))
            except BaseException as exc:
                raise _native_error(exc) from exc
            if removed:
                self._deployment_matches_committed = False
                self._deployment_match_known = False
                self._committed_deployment_plan = None
                self._deployed_entries_cache = None
                self._deployment_projection_cache.clear()
        return removed

    def export_legacy_maps(self, output_dir: Path) -> tuple[Path, Path]:
        """CLI compatibility projection from one pinned generation."""
        from Utils.atomic_write import write_atomic_text
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        normal: list[tuple[str, str]] = []
        root: list[tuple[str, str]] = []
        for winner in self.snapshot().export().winners:
            if winner.namespace == "archive" or not winner.legacy_rel:
                continue
            target = root if winner.legacy_root else normal
            target.append((winner.legacy_rel, winner.mod_name))
        normal.sort(key=lambda row: row[0].lower())
        root.sort(key=lambda row: row[0].lower())
        normal_path = output_dir / "filemap.txt"
        root_path = output_dir / "filemap_root.txt"
        write_atomic_text(
            normal_path,
            "".join(f"{path}\t{owner}\n" for path, owner in normal),
            errors="surrogateescape",
        )
        write_atomic_text(
            root_path,
            "".join(f"{path}\t{owner}\n" for path, owner in root),
            errors="surrogateescape",
        )
        return normal_path, root_path


class LibrarySession:
    __slots__ = (
        "game", "root", "log", "_native", "_profiles", "_refresh_lock",
        "_variant_keys_cache", "__weakref__",
    )

    def __init__(self, game, root: Path, *, log_fn=None):
        self.game = game
        self.root = Path(root)
        self.log = log_fn or (lambda _message: None)
        native = require_native()
        try:
            self._native = native.LibrarySession.open(self.root)
        except BaseException as exc:
            if "corrupt" not in str(exc).lower() and "malformed" not in str(exc).lower():
                raise _native_error(exc) from exc
            quarantined = self._quarantine_corrupt_database()
            self.log(
                f"Filegraph catalog was corrupt and has been quarantined at "
                f"{quarantined}. A raw manifest rebuild is required.")
            self._native = native.LibrarySession.open(self.root)
            self._native.set_ready(False)
        self._profiles: dict[str, ProfileSession] = {}
        self._refresh_lock = threading.Lock()
        self._variant_keys_cache: dict[str, frozenset[str]] | None = None

    def rebind_game(self, game, *, log_fn=None) -> None:
        """Bind cached profile sessions to the current game-handler object."""
        if self.game is game:
            if log_fn is not None:
                self.log = log_fn
                for profile in self._profiles.values():
                    profile.adapter.log = log_fn
            return
        with self._refresh_lock:
            if log_fn is not None:
                self.log = log_fn
            if self.game is game:
                for profile in self._profiles.values():
                    profile.adapter.log = self.log
                return
            self.game = game
            for profile in self._profiles.values():
                profile._rebind_game(game, self.log)

    def _quarantine_corrupt_database(self) -> Path:
        database = self.root / "filegraph.sqlite3"
        stamp = time.strftime("%Y%m%d-%H%M%S")
        target = self.root / f"filegraph.corrupt-{stamp}-{os.getpid()}.sqlite3"
        if database.exists():
            os.replace(database, target)
        for suffix in ("-wal", "-shm"):
            companion = Path(str(database) + suffix)
            if companion.exists():
                os.replace(companion, Path(str(target) + suffix))
        return target

    @property
    def database_path(self) -> Path:
        return Path(self._native.database_path)

    def status(self) -> CatalogStatus:
        try:
            return CatalogStatus.from_wire(unpack(self._native.status()))
        except BaseException as exc:
            raise _native_error(exc) from exc

    def open_profile(self, profile_dir: Path) -> ProfileSession:
        key = str(Path(profile_dir).resolve(strict=False))
        session = self._profiles.get(key)
        if session is None:
            session = ProfileSession(self, Path(profile_dir))
            self._profiles[key] = session
        return session

    def replace_mod_manifest(
        self, batch: dict, *, cancel: CancellationToken | None = None,
    ) -> int:
        try:
            generation = int(self._native.replace_mod_manifest(
                pack(batch), cancel._native if cancel is not None else None))
            self._variant_keys_cache = None
            for profile in self._profiles.values():
                profile._invalidate_resolution_cache()
            return generation
        except BaseException as exc:
            raise _native_error(exc) from exc

    def manifest_fingerprints(self) -> dict[str, bytes]:
        try:
            return {
                str(name): bytes(fingerprint)
                for name, fingerprint in unpack(
                    self._native.manifest_fingerprints()).items()
            }
        except BaseException as exc:
            raise _native_error(exc) from exc

    def manifest_for_rederive(self, mod_name: str) -> dict | None:
        try:
            value = unpack(
                self._native.manifest_for_rederive(mod_name.lower()))
            return dict(value) if value is not None else None
        except BaseException as exc:
            raise _native_error(exc) from exc

    def variant_keys(self) -> dict[str, frozenset[str]]:
        if self._variant_keys_cache is not None:
            return dict(self._variant_keys_cache)
        try:
            variants = {
                str(name): frozenset(map(str, variants))
                for name, variants in unpack(self._native.variant_keys()).items()
            }
            self._variant_keys_cache = variants
            return dict(variants)
        except BaseException as exc:
            raise _native_error(exc) from exc

    def archive_units(
        self, selected_variants: Iterable[tuple[str, str]],
    ) -> tuple[tuple, ...]:
        try:
            return tuple(
                tuple(record) for record in unpack(
                    self._native.archive_units(pack(list(selected_variants))))
            )
        except BaseException as exc:
            raise _native_error(exc) from exc

    def remove_mod(self, mod_name: str) -> bool:
        try:
            removed = bool(self._native.remove_mod(mod_name.lower()))
            if removed:
                self._variant_keys_cache = None
                for profile in self._profiles.values():
                    profile._invalidate_resolution_cache()
            return removed
        except BaseException as exc:
            raise _native_error(exc) from exc

    def rename_mod(self, old_name: str, new_name: str) -> bool:
        try:
            renamed = bool(self._native.rename_mod(
                old_name.lower(), new_name.lower(), new_name))
            if renamed:
                self._variant_keys_cache = None
                for profile in self._profiles.values():
                    profile._invalidate_resolution_cache()
            return renamed
        except BaseException as exc:
            raise _native_error(exc) from exc

    def refresh(
        self,
        profile_dir: Path,
        *,
        progress: Callable | None = None,
        cancel: CancellationToken | None = None,
        mod_names: Iterable[str] | None = None,
    ) -> CatalogStatus:
        """Authoritatively reconcile raw disk manifests into the catalog."""
        if mod_names is None:
            return self.rebuild(
                profile_dir, progress=progress, cancel=cancel)
        # A targeted transaction is valid only on top of a complete catalog.
        # Treat the first manager-owned mutation as first migration instead
        # of publishing a catalog that contains only the touched mod.
        if mod_names is not None and not self.status().ready:
            return self.rebuild(
                profile_dir, progress=progress, cancel=cancel)
        token = cancel or CancellationToken()
        with self._refresh_lock:
            session = self.open_profile(profile_dir)
            batches = session.adapter.refresh_batches(
                mod_names, progress=progress, cancel=token)
            if token.is_cancelled():
                raise FileGraphCancelled("filegraph refresh cancelled")
            build_root = Path(tempfile.mkdtemp(
                prefix=".filegraph-refresh-", dir=self.root))
            temporary = None
            try:
                self._native.checkpoint()
                shutil.copy2(
                    self.database_path, build_root / "filegraph.sqlite3")
                temporary = require_native().LibrarySession.open(build_root)
                temporary.set_ready(False)
                for batch in batches:
                    if token.is_cancelled():
                        raise FileGraphCancelled("filegraph refresh cancelled")
                    temporary.replace_mod_manifest(pack(batch), token._native)
                temporary.set_ready(True)
                temporary.checkpoint()
                self._native.activate_catalog(temporary.database_path)
                self._variant_keys_cache = None
                for profile in self._profiles.values():
                    profile._invalidate_resolution_cache()
                return self.status()
            except BaseException as exc:
                if isinstance(exc, FileGraphCancelled):
                    raise
                raise _native_error(exc) from exc
            finally:
                temporary = None
                shutil.rmtree(build_root, ignore_errors=True)

    def rebuild(
        self,
        profile_dir: Path,
        *,
        progress: Callable | None = None,
        cancel: CancellationToken | None = None,
    ) -> CatalogStatus:
        """Build, validate, and atomically activate a complete raw catalog."""
        with self._refresh_lock:
            session = self.open_profile(profile_dir)
            session.adapter._refresh_profile_rules()
            token = cancel or CancellationToken()
            build_root = Path(tempfile.mkdtemp(
                prefix=".filegraph-build-", dir=self.root))
            temporary = None
            try:
                native = require_native()
                temporary = native.LibrarySession.open(build_root)
                batches = session.adapter.refresh_batches(
                    progress=progress, cancel=token)
                for batch in batches:
                    if token.is_cancelled():
                        raise FileGraphCancelled("filegraph rebuild cancelled")
                    temporary.replace_mod_manifest(pack(batch), token._native)
                temporary.set_ready(True)
                temporary.checkpoint()
                self._native.activate_catalog(
                    temporary.database_path, True)
                self._variant_keys_cache = None
                for profile_id, profile in self._profiles.items():
                    profile._reset_after_catalog_rebuild(profile_id)
                return self.status()
            except BaseException as exc:
                if isinstance(exc, FileGraphCancelled):
                    raise
                raise _native_error(exc) from exc
            finally:
                temporary = None
                shutil.rmtree(build_root, ignore_errors=True)

    def ensure_ready(
        self, profile_dir: Path, *, progress: Callable | None = None,
        cancel: CancellationToken | None = None,
    ) -> CatalogStatus:
        status = self.status()
        if status.ready:
            return status
        return self.rebuild(profile_dir, progress=progress, cancel=cancel)


_library_sessions: "weakref.WeakValueDictionary[str, LibrarySession]" = (
    weakref.WeakValueDictionary())
_library_guard = threading.Lock()


class FileGraphService:
    """Factory hiding native/SQLite ownership and sharing one session per library."""

    @staticmethod
    def open_library(game, profile_dir: Path, *, log_fn=None) -> LibrarySession:
        profile_dir = Path(profile_dir)
        try:
            from Utils.profile_state import profile_uses_specific_mods
            specific = profile_uses_specific_mods(profile_dir)
        except Exception:
            specific = False
        if specific:
            staging = profile_dir / "mods"
        else:
            getter = getattr(game, "get_mod_staging_path", None)
            staging = Path(getter() if callable(getter)
                           else game.get_effective_mod_staging_path())
        root = staging.parent
        try:
            key = str(root.resolve(strict=False))
        except OSError:
            key = str(root)
        with _library_guard:
            existing = _library_sessions.get(key)
            if existing is None:
                session = LibrarySession(game, root, log_fn=log_fn)
                _library_sessions[key] = session
                return session
        existing.rebind_game(game, log_fn=log_fn)
        return existing


def open_profile(game, profile_dir: Path, *, log_fn=None) -> ProfileSession:
    return FileGraphService.open_library(
        game, profile_dir, log_fn=log_fn).open_profile(profile_dir)


def plugin_source_paths(snapshot: ResolvedSnapshot | None, game) -> dict[str, Path]:
    """Resolve winning staged plugins from one pinned Filegraph generation."""
    if snapshot is None:
        return {}
    staging = Path(game.get_effective_mod_staging_path())
    overwrite = Path(game.get_effective_overwrite_path())
    root_folder = Path(game.get_effective_root_folder_path())
    result: dict[str, Path] = {}
    for plugin, winner in snapshot.plugin_winners().items():
        if winner.mod_key == "[overwrite]":
            source_root = overwrite
        elif winner.mod_key == "[root_folder]":
            source_root = root_folder
        else:
            source_root = staging / winner.mod_name
        relative = bytes(winner.source_rel).decode("utf-8", "surrogateescape")
        result[plugin.lower()] = source_root / relative
    return result


def active_snapshot(game) -> ResolvedSnapshot:
    """Return the active profile's restored graph, reconciling only if absent."""
    profile_dir = getattr(game, "_active_profile_dir", None)
    if profile_dir is None:
        profile_dir = Path(game.get_profile_root()) / "profiles" / "default"
    profile_dir = Path(profile_dir)
    library = FileGraphService.open_library(game, profile_dir)
    status = library.ensure_ready(profile_dir)
    profile = library.open_profile(profile_dir)
    snapshot = profile.snapshot()
    if (snapshot.generation == 0
            or snapshot.inventory_generation != status.inventory_generation):
        profile.reconcile(operation_hint={"kind": "restore_snapshot"})
        snapshot = profile.snapshot()
    return snapshot


def source_path(game, mod_name: str, relative: bytes | str) -> Path:
    """Map an exact catalog source identity back to its effective staging root."""
    key = mod_name.lower()
    if key == "[overwrite]":
        root = Path(game.get_effective_overwrite_path())
    elif key == "[root_folder]":
        root = Path(game.get_effective_root_folder_path())
    else:
        root = Path(game.get_effective_mod_staging_path()) / mod_name
    text = (bytes(relative).decode("utf-8", "surrogateescape")
            if isinstance(relative, (bytes, bytearray)) else str(relative))
    return root / text


__all__ = [
    "CancellationToken", "CatalogStatus", "ConflictSummary", "DeployEntry",
    "DeploymentPlan", "FileGraphService", "LibrarySession", "ProfileSession",
    "ResolvedSnapshot", "ResolutionDelta", "open_profile",
    "active_snapshot", "plugin_source_paths", "source_path",
    "FLAG_ARCHIVE", "FLAG_FRAMEWORK", "FLAG_PLUGIN", "FLAG_PRE_RTX",
    "FLAG_ROOT_RULE",
    "FLAG_TEXT",
]
