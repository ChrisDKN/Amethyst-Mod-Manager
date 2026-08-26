"""Typed Python contracts for the native filegraph engine.

The native boundary intentionally uses MessagePack batches instead of one
Python call per file.  These small immutable wrappers keep MessagePack and
PyO3 details out of the UI, deploy handlers, and game adapters.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Iterator


def _text_from_bytes(value: bytes) -> str:
    return bytes(value).decode("utf-8", "surrogateescape")


@dataclass(frozen=True, slots=True)
class CatalogStatus:
    schema_version: int
    api_version: int
    inventory_generation: int
    engine_revision: int
    rules_revision: int
    ready: bool
    mod_count: int
    candidate_count: int

    @classmethod
    def from_wire(cls, value: dict[str, Any]) -> "CatalogStatus":
        return cls(**{name: value[name] for name in cls.__dataclass_fields__})


@dataclass(frozen=True, slots=True)
class ConflictSummary:
    loose_code: int = 0
    archive_code: int = 0
    identity_code: int = 0
    loose_wins: int = 0
    loose_losses: int = 0
    loose_surviving: int = 0
    archive_wins: int = 0
    archive_losses: int = 0
    archive_surviving: int = 0
    identity_wins: int = 0
    identity_losses: int = 0
    flags: int = 0

    @classmethod
    def from_wire(cls, value: dict[str, Any] | None) -> "ConflictSummary":
        if not value:
            return cls()
        return cls(**{
            name: value.get(name, field_info.default)
            for name, field_info in cls.__dataclass_fields__.items()
        })


@dataclass(frozen=True, slots=True)
class Winner:
    candidate_id: int
    mod_name: str
    mod_key: str
    target: str
    destination_key: bytes
    destination_display: str
    source_rel: bytes
    source_display: str
    namespace: str
    legacy_root: bool
    legacy_rel: str
    flags: int = 0

    @classmethod
    def from_wire(cls, value: dict[str, Any]) -> "Winner":
        value = dict(value)
        value["destination_key"] = bytes(value["destination_key"])
        value["source_rel"] = bytes(value["source_rel"])
        return cls(**value)

    @property
    def destination(self) -> str:
        return self.destination_display or _text_from_bytes(self.destination_key)


@dataclass(frozen=True, slots=True)
class Provider:
    candidate_id: int
    mod_name: str
    kind: str
    winning: bool

    @classmethod
    def from_wire(cls, value: dict[str, Any]) -> "Provider":
        return cls(**value)


@dataclass(frozen=True, slots=True)
class ModFile:
    candidate_id: int
    mod_name: str
    source_rel: bytes
    source_display: str
    target: str
    destination_key: bytes
    destination_display: str
    namespace: str
    provider_kind: str
    enabled: bool
    winning: bool
    conflict_status: int
    deployable: bool
    flags: int = 0
    plugin_key: str | None = None
    legacy_rel: str = ""

    @classmethod
    def from_wire(cls, value: dict[str, Any]) -> "ModFile":
        value = dict(value)
        value["destination_key"] = bytes(value["destination_key"])
        value["source_rel"] = bytes(value["source_rel"])
        return cls(**value)

    @property
    def source(self) -> str:
        return self.source_display or _text_from_bytes(self.source_rel)

    @property
    def destination(self) -> str:
        return self.destination_display or _text_from_bytes(self.destination_key)


@dataclass(frozen=True, slots=True)
class ConflictEdge:
    kind: str
    loser: str
    winner: str
    refcount: int

    @classmethod
    def from_wire(cls, value: dict[str, Any]) -> "ConflictEdge":
        return cls(**value)


@dataclass(frozen=True, slots=True)
class ResolutionDelta:
    base_generation: int
    generation: int
    inventory_generation: int
    full_rebuild: bool
    candidates_touched: int
    destinations_touched: int
    graph_compute_ns: int = 0
    sqlite_commit_ns: int = 0
    changed_winner_ids: tuple[int, ...] = ()
    removed_winner_ids: tuple[int, ...] = ()
    touched_winner_ids: tuple[int, ...] = ()
    changed_summaries: dict[str, ConflictSummary] = field(default_factory=dict)
    changed_plugin_owners: dict[str, str | None] = field(default_factory=dict)
    changed_capability_flags: dict[str, int | None] = field(default_factory=dict)
    changed_edges: tuple[ConflictEdge, ...] = ()
    deployment_dirty: bool = False

    @classmethod
    def from_wire(cls, value: dict[str, Any]) -> "ResolutionDelta":
        changed_winner_ids = tuple(value.get("changed_winner_ids", ()))
        touched_winner_ids = tuple(value.get("touched_winner_ids", ()))
        if not touched_winner_ids and changed_winner_ids:
            touched_winner_ids = changed_winner_ids
        return cls(
            base_generation=int(value.get("base_generation", 0)),
            generation=int(value.get("generation", 0)),
            inventory_generation=int(value.get("inventory_generation", 0)),
            full_rebuild=bool(value.get("full_rebuild", False)),
            candidates_touched=int(value.get("candidates_touched", 0)),
            destinations_touched=int(value.get("destinations_touched", 0)),
            graph_compute_ns=int(value.get("graph_compute_ns", 0)),
            sqlite_commit_ns=int(value.get("sqlite_commit_ns", 0)),
            changed_winner_ids=changed_winner_ids,
            removed_winner_ids=tuple(value.get("removed_winner_ids", ())),
            touched_winner_ids=touched_winner_ids,
            changed_summaries={
                name: ConflictSummary.from_wire(summary)
                for name, summary in value.get("changed_summaries", {}).items()
            },
            changed_plugin_owners=dict(value.get("changed_plugin_owners", {})),
            changed_capability_flags={
                name: None if flags is None else int(flags)
                for name, flags in value.get(
                    "changed_capability_flags", {}).items()
            },
            changed_edges=tuple(
                ConflictEdge.from_wire(edge)
                for edge in value.get("changed_edges", ())
            ),
            deployment_dirty=bool(value.get("deployment_dirty", False)),
        )


@dataclass(frozen=True, slots=True)
class SnapshotExport:
    generation: int
    inventory_generation: int
    winners: tuple[Winner, ...]
    summaries: dict[str, ConflictSummary]
    edges: tuple[ConflictEdge, ...]
    plugin_owners: dict[str, str]
    capability_flags: dict[str, int]

    @classmethod
    def from_wire(cls, value: dict[str, Any]) -> "SnapshotExport":
        return cls(
            generation=int(value.get("generation", 0)),
            inventory_generation=int(value.get("inventory_generation", 0)),
            winners=tuple(Winner.from_wire(v) for v in value.get("winners", ())),
            summaries={
                name: ConflictSummary.from_wire(summary)
                for name, summary in value.get("summaries", {}).items()
            },
            edges=tuple(ConflictEdge.from_wire(v) for v in value.get("edges", ())),
            plugin_owners=dict(value.get("plugin_owners", {})),
            capability_flags={
                name: int(flags)
                for name, flags in value.get("capability_flags", {}).items()
            },
        )


@dataclass(frozen=True, slots=True)
class ConflictState:
    generation: int
    summaries: dict[str, ConflictSummary]
    edges: tuple[ConflictEdge, ...]
    plugin_owners: dict[str, str]
    archive_plugin_stems: dict[str, frozenset[str]]
    capability_flags: dict[str, int]

    @classmethod
    def from_wire(cls, value: dict[str, Any]) -> "ConflictState":
        return cls(
            generation=int(value.get("generation", 0)),
            summaries={
                name: ConflictSummary.from_wire(summary)
                for name, summary in value.get("summaries", {}).items()
            },
            edges=tuple(
                ConflictEdge.from_wire(edge) for edge in value.get("edges", ())
            ),
            plugin_owners=dict(value.get("plugin_owners", {})),
            archive_plugin_stems={
                name: frozenset(stems)
                for name, stems in value.get("archive_plugin_stems", {}).items()
            },
            capability_flags={
                name: int(flags)
                for name, flags in value.get("capability_flags", {}).items()
            },
        )


@dataclass(frozen=True, slots=True)
class DeployEntry:
    candidate_id: int
    mod_name: str
    mod_key: str
    provider_kind: str
    target: str
    destination_key: bytes
    destination_display: str
    source_rel: bytes
    source_display: str
    source_fingerprint: bytes
    legacy_root: bool
    legacy_rel: str
    flags: int = 0
    source_root: Path | None = None
    link_mode: str | None = None

    @classmethod
    def from_wire(
        cls, value: dict[str, Any] | list | tuple,
        *, source_root: Path | None = None,
    ) -> "DeployEntry":
        if not isinstance(value, dict):
            return cls.from_compact(value, source_root=source_root)
        fields = dict(value)
        for name in ("destination_key", "source_rel", "source_fingerprint"):
            fields[name] = bytes(fields[name])
        if source_root is not None:
            fields["source_root"] = source_root
        return cls(**fields)

    @classmethod
    def from_compact(
        cls, value: list | tuple, *, source_root: Path | None = None,
    ) -> "DeployEntry":
        if len(value) != 13:
            raise ValueError(
                f"invalid compact deployment entry field count: {len(value)}")
        return cls(
            candidate_id=int(value[0]),
            mod_name=str(value[1]),
            mod_key=str(value[2]),
            provider_kind=str(value[3]),
            target=str(value[4]),
            destination_key=bytes(value[5]),
            destination_display=str(value[6]),
            source_rel=bytes(value[7]),
            source_display=str(value[8]),
            source_fingerprint=bytes(value[9]),
            legacy_root=bool(value[10]),
            legacy_rel=str(value[11]),
            flags=int(value[12]),
            source_root=source_root,
        )

    @property
    def destination(self) -> str:
        return self.destination_display or _text_from_bytes(self.destination_key)

    @property
    def source_path(self) -> Path | None:
        if self.source_root is None:
            return None
        return self.source_root / _text_from_bytes(self.source_rel)


@dataclass(frozen=True, slots=True)
class DeploymentPlan:
    generation: int
    inventory_generation: int
    entries: tuple[DeployEntry, ...]

    @classmethod
    def from_wire(
        cls, value: dict[str, Any] | list | tuple,
        *, source_root_for_mod=None,
    ) -> "DeploymentPlan":
        if isinstance(value, dict):
            generation = int(value.get("generation", 0))
            inventory_generation = int(value.get("inventory_generation", 0))
            values = value.get("entries", ())
        else:
            if len(value) != 3:
                raise ValueError(
                    f"invalid compact deployment plan field count: {len(value)}")
            generation = int(value[0])
            inventory_generation = int(value[1])
            values = value[2]

        def convert(entry):
            if isinstance(entry, dict):
                mod_name = str(entry.get("mod_name", ""))
            else:
                mod_name = str(entry[1])
            source_root = (
                source_root_for_mod(mod_name)
                if source_root_for_mod is not None else None
            )
            return DeployEntry.from_wire(entry, source_root=source_root)

        return cls(
            generation=generation,
            inventory_generation=inventory_generation,
            entries=tuple(convert(entry) for entry in values),
        )

    def for_target(self, target: str) -> Iterator[DeployEntry]:
        return (entry for entry in self.entries if entry.target == target)


@dataclass(frozen=True, slots=True)
class OperationRecord:
    operation_id: str
    profile_id: str
    kind: str
    state: str
    phase: str
    generation: int
    created_ns: int
    updated_ns: int

    @classmethod
    def from_wire(cls, value: dict[str, Any]) -> "OperationRecord":
        return cls(**value)


@dataclass(frozen=True, slots=True)
class DeployedStateEntry:
    target: str
    destination_key: bytes
    destination_display: str
    candidate_id: int
    mod_name: str
    mod_key: str
    provider_kind: str
    source_rel: bytes
    source_display: str
    source_fingerprint: bytes
    link_mode: str
    deployed_generation: int

    @classmethod
    def from_wire(cls, value: dict[str, Any]) -> "DeployedStateEntry":
        value = dict(value)
        for name in ("destination_key", "source_rel", "source_fingerprint"):
            value[name] = bytes(value[name])
        return cls(**value)

    @property
    def destination(self) -> str:
        return self.destination_display or _text_from_bytes(self.destination_key)


@dataclass(frozen=True, slots=True)
class RefreshProgress:
    mods_scanned: int
    mods_total: int
    files_scanned: int
    archives_scanned: int
    current_mod: str = ""


class FileGraphCancelled(RuntimeError):
    """Raised when a refresh or reconcile cancellation token is observed."""


class FileGraphUnavailable(RuntimeError):
    """The required native component is missing or has the wrong API version."""


class FileGraphStale(RuntimeError):
    """Authoritative intent changed but its graph transaction did not commit."""


class FileGraphRecoveryRequired(RuntimeError):
    """A prior deployment journal must be recovered before graph mutation."""


class FileGraphBusy(RuntimeError):
    """Another process owns the writer lock for this mod library."""


def partner_maps(
    edges: Iterable[ConflictEdge], kinds: set[str]
) -> tuple[dict[str, set[str]], dict[str, set[str]]]:
    """Return legacy winner→losers and loser→winners maps for selected kinds."""
    overrides: dict[str, set[str]] = {}
    overridden_by: dict[str, set[str]] = {}
    for edge in edges:
        if edge.kind not in kinds:
            continue
        overrides.setdefault(edge.winner, set()).add(edge.loser)
        overridden_by.setdefault(edge.loser, set()).add(edge.winner)
    return overrides, overridden_by
