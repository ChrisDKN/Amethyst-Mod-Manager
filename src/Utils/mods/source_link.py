"""GUI-neutral core for linking hand-installed mods to their source page.

The link is the identity: providers parse and stamp native ``meta.ini`` data,
and this module only owns the informational ``[sourceLink]`` marker.
"""

from __future__ import annotations

import configparser
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlparse

from Thunderstore.thunderstore_identify import identify_all
from Thunderstore.thunderstore_meta import read_meta as read_thunderstore_meta
from Utils.atomic_write import atomic_writer
from Utils.bg3.modio_loader import load_modio_module
from Utils.mods.copy import resolve_target_staging
from Utils.mods.metadata import meta_file_identity, meta_file_lock


class SourceLinkError(ValueError):
    def __init__(self, code: str, message: str, details: dict | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}


@dataclass(frozen=True)
class LinkCandidate:
    provider: str
    url: str
    name: str
    identity: dict
    details: dict


@dataclass(frozen=True)
class VersionChoice:
    id: str
    label: str
    version: str
    recommended: bool
    payload: dict


@dataclass(frozen=True)
class ResolvedLink:
    candidate: LinkCandidate
    versions: tuple[VersionChoice, ...] | list[VersionChoice]
    verification: str
    local_version: str
    warning: str
    meta_token: tuple | None


@dataclass(frozen=True)
class LinkResult:
    mod: str
    provider: str
    url: str
    version: str
    verification: str
    display_name: str
    missing_thunderstore: Any
    missing_plugin: Any
    message: str
    # Optional identity details for the UI. Defaults keep existing constructors
    # (tests, stubs) working; the orchestrator fills them from the candidate.
    name: str = ""
    community: str = ""


class LinkAdapter(Protocol):
    key: str

    def matches(self, url: str, game) -> bool: ...

    def parse(self, url: str, game, *, api=None) -> LinkCandidate: ...

    def versions(self, candidate: LinkCandidate, game, *, api=None): ...

    def stamp(self, meta_path: Path, candidate: LinkCandidate,
              choice: VersionChoice, *, confirm_replace: bool = False) -> None: ...

    def verify(self, mod_dir: Path, candidate: LinkCandidate,
               choice: VersionChoice, *, game=None, api=None) -> tuple[str, str]: ...

    def followup(self, game, profile: Path, mod_name: str,
                 candidate: LinkCandidate, choice: VersionChoice, *, api=None, log=None): ...


def read_source_link(meta_path: Path) -> dict:
    """Return the ``[sourceLink]`` marker, or ``{}`` when absent/unreadable."""
    parser = configparser.ConfigParser(interpolation=None)
    try:
        parser.read(meta_path, encoding="utf-8")
    except (configparser.Error, OSError, ValueError):
        return {}
    if not parser.has_section("sourceLink"):
        return {}
    return dict(parser.items("sourceLink"))


def write_source_link(meta_path: Path, *, url: str, provider: str,
                      verification: str) -> None:
    """Set the ``[sourceLink]`` marker, preserving every other section/key."""
    meta_path = Path(meta_path)
    with meta_file_lock(meta_path):
        # Match the native readers: a stray no-value key or duplicate section
        # must not stop the marker write that follows a successful stamp.
        parser = configparser.ConfigParser(
            interpolation=None, strict=False, allow_no_value=True)
        try:
            parser.read(meta_path, encoding="utf-8")
        except (configparser.Error, OSError, ValueError):
            pass
        if not parser.has_section("sourceLink"):
            parser.add_section("sourceLink")
        parser.set("sourceLink", "url", url)
        parser.set("sourceLink", "provider", provider)
        parser.set("sourceLink", "verification", verification)
        parser.set("sourceLink", "linked",
                   datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S"))
        with atomic_writer(meta_path) as handle:
            parser.write(handle)


def adapters() -> tuple[LinkAdapter, ...]:
    # Local import: stores import these models, so importing them at module
    # scope here would create a cycle for callers that import the stores first.
    from Utils.mods.source_link_plugin import PluginProviderAdapter
    from Utils.mods.source_link_stores import (
        ModioAdapter, NexusAdapter, ThunderstoreAdapter, WorkshopAdapter,
    )

    return (NexusAdapter(), ThunderstoreAdapter(), ThunderstoreAdapter("hexium"),
            ModioAdapter(), WorkshopAdapter(), PluginProviderAdapter())


def detect_adapter(url: str, game) -> LinkAdapter:
    # Public provider links must be HTTP(S). Manager-plugin source URIs are the
    # only exception - their own loader validates the scheme and host.
    http = urlparse(url).scheme.lower() in ("http", "https")
    for adapter in adapters():
        if not http and getattr(adapter, "key", None) != "plugin":
            continue
        if adapter.matches(url, game):
            return adapter
    raise SourceLinkError(
        "unsupported_source",
        "That link is not from a supported mod source. Use Custom URL → Set URL instead.",
        {"url": url},
    )


def _api_for(adapter: LinkAdapter, api):
    """The Nexus client is the only injected API a provider adapter may use."""
    return api if getattr(adapter, "key", None) == "nexus" else None


def _choice_by_id(resolved: ResolvedLink, choice_id: str) -> VersionChoice:
    choice = next((v for v in resolved.versions if v.id == choice_id), None)
    if choice is None:
        raise SourceLinkError(
            "version_not_found", "The selected version is no longer available.",
            {"choice_id": choice_id, "url": resolved.candidate.url})
    return choice


def resolve_link(url: str, game, mod_dir: Path, *, api=None) -> ResolvedLink:
    """Parse a source link and preview the recommended version without writing."""
    adapter = detect_adapter(url, game)
    provider_api = _api_for(adapter, api)
    candidate = adapter.parse(url, game, api=provider_api)
    versions = tuple(adapter.versions(candidate, game, api=provider_api))
    if not versions:
        raise SourceLinkError(
            "no_versions", "No versions are available for that source.", {"url": url})
    preview = next((v for v in versions if v.recommended), versions[0])
    verification, local_version = adapter.verify(
        mod_dir, candidate, preview, game=game, api=provider_api)
    meta_path = Path(mod_dir) / "meta.ini"
    meta_token = meta_file_identity(meta_path) if meta_path.exists() else None
    return ResolvedLink(
        candidate=candidate,
        versions=versions,
        verification=verification,
        local_version=local_version,
        warning="",
        meta_token=meta_token,
    )


def verify_choice(game, mod_dir: Path, resolved: ResolvedLink, choice_id: str,
                  *, api=None) -> tuple[str, str]:
    """Re-verify one already-resolved choice (used when the dropdown changes)."""
    adapter = detect_adapter(resolved.candidate.url, game)
    choice = _choice_by_id(resolved, choice_id)
    return adapter.verify(mod_dir, resolved.candidate, choice, game=game,
                          api=_api_for(adapter, api))


def apply_link(game, profile: Path, mod_dir: Path, resolved: ResolvedLink,
               choice_id: str, *, api=None, confirm_replace: bool = False,
               log=None) -> LinkResult:
    """Stamp the selected source identity into ``meta.ini`` and link the mod.

    Post-link dependency work runs outside the metadata lock and is best effort:
    a failure is reported in the result but never rolls back the stamped identity.
    """
    mod_dir = Path(mod_dir)
    name = mod_dir.name
    meta_path = mod_dir / "meta.ini"

    if mod_dir.is_symlink() or meta_path.is_symlink() or (not name or name in (".", "..")
                                                          or Path(name).name != name):
        raise SourceLinkError(
            "unsafe_mod_path", "The mod folder or its metadata path is unsafe.",
            {"name": name, "path": str(mod_dir)})

    adapter = detect_adapter(resolved.candidate.url, game)
    provider_api = _api_for(adapter, api)
    choice = _choice_by_id(resolved, choice_id)
    verification, _local_version = verify_choice(
        game, mod_dir, resolved, choice_id, api=api)

    with meta_file_lock(meta_path):
        current = meta_file_identity(meta_path) if meta_path.exists() else None
        if current != resolved.meta_token:
            raise SourceLinkError(
                "stale_source_link",
                "The mod metadata changed since the link was previewed. Please review again.",
                {"url": resolved.candidate.url})
        adapter.stamp(meta_path, resolved.candidate, choice,
                      confirm_replace=confirm_replace)
        # Plugin source URIs can embed an access token; the marker is informational
        # and is never used to re-resolve, so persist only the provider identity.
        marker_url = resolved.candidate.url
        if resolved.candidate.provider == "plugin":
            identity = resolved.candidate.identity
            marker_url = "plugin:{0}/{1}".format(
                identity.get("plugin_id", ""), identity.get("provider_id", ""))
        write_source_link(meta_path, url=marker_url,
                          provider=resolved.candidate.provider,
                          verification=verification)

    missing_thunderstore, missing_plugin = (), ()
    message = ""
    try:
        missing_thunderstore, missing_plugin = adapter.followup(
            game, profile, name, resolved.candidate, choice,
            api=provider_api, log=log)
    except Exception as exc:
        message = f"Linked, but post-link checks failed: {exc}"
        (log or (lambda _message: None))(message)

    return LinkResult(
        mod=name,
        provider=resolved.candidate.provider,
        url=resolved.candidate.url,
        version=choice.version,
        verification=verification,
        display_name=choice.label,
        missing_thunderstore=tuple(missing_thunderstore or ()),
        missing_plugin=tuple(missing_plugin or ()),
        message=message,
        name=resolved.candidate.name,
        community=resolved.candidate.identity.get("community", ""),
    )


@dataclass(frozen=True)
class BatchIdentification:
    """One installed folder's outcome from the best-effort batch pass."""

    mod: str
    provider: str
    matched: bool
    reason: str


_PATH_SEPARATORS = tuple(s for s in (os.sep, os.altsep, "/", "\\") if s)


def identify_installed_sources(
        game, profile: Path, *, progress=None,
        cancel=None) -> list[BatchIdentification]:
    """Stamp only sources already carried by installed files.

    Manifest-bearing Thunderstore/Hexium mods are adopted by the existing
    ``identify_all``; BG3 mod.io mods are resolved from the PublishHandle embedded
    in their pak. Steam Workshop metadata is reported but never invented, and
    Nexus/plugin mods are left for a manual link. Post-link followups are never
    run - this is a metadata-only pass.
    """
    staging = Path(resolve_target_staging(game, Path(profile)))
    rows: list[BatchIdentification] = []
    handled: set[str] = set()

    def _note(message: str) -> None:
        if progress is not None:
            progress(message)

    def _cancelled() -> bool:
        return cancel is not None and bool(cancel())

    # One manifest scan per configured community. identify_all owns the parsing
    # and the stamping; we only translate its results into report rows. An empty
    # community means the source is unconfigured - skip it entirely rather than
    # letting manifest/folder-name heuristics stamp an unconfigured source.
    from Utils.mods.modlist import _RESERVED_STAGING_NAMES

    for source in ("thunderstore", "hexium"):
        community = getattr(game, f"{source}_community", "") or ""
        if not community:
            continue
        if _cancelled():
            break
        _note(f"identifying {source} mods")
        for result in identify_all(
                staging, community, source=source, save=True,
                progress_cb=_note):
            rows.append(BatchIdentification(
                mod=result.mod_name,
                provider=getattr(result, "source", "") or source,
                matched=bool(result.matched),
                reason=getattr(result, "reason", "") or ""))
            handled.add(result.mod_name)

    # mod.io resolution needs both a BG3 game and configured credentials.
    modio = None
    if getattr(game, "game_id", None) == "baldurs_gate_3":
        try:
            key, path = load_modio_module("modio_key").load_modio_credentials()
        except Exception:
            key, path = "", ""
        if key and path:
            modio = (load_modio_module("modio_meta"), key, path)

    for folder in sorted(staging.iterdir(), key=lambda item: item.name):
        if folder.is_symlink() or not folder.is_dir():
            continue
        name = folder.name
        if (not name or name in (".", "..") or Path(name).name != name
                or any(sep in name for sep in _PATH_SEPARATORS)):
            continue
        if name.lower() in _RESERVED_STAGING_NAMES or name.lower().endswith("_separator"):
            continue
        if name in handled:
            continue
        if _cancelled():
            break
        _note(f"checking {name}")
        meta_path = folder / "meta.ini"

        # A previously stamped Thunderstore/Hexium mod may no longer have its
        # manifest, so identify_all would skip it; its native meta is still proof.
        ts_meta = read_thunderstore_meta(meta_path)
        if ts_meta.package_id:
            rows.append(BatchIdentification(
                mod=name, provider=ts_meta.source or "thunderstore",
                matched=True, reason=""))
            continue

        parser = configparser.ConfigParser(interpolation=None)
        try:
            parser.read(meta_path, encoding="utf-8")
        except (configparser.Error, OSError, ValueError):
            pass

        if modio is not None:
            modio_module, key, path = modio
            has_id = False
            if parser.has_section("modio"):
                try:
                    has_id = int(parser.get("modio", "modId", fallback="0") or 0) > 0
                except (TypeError, ValueError):
                    has_id = False
            if has_id:
                rows.append(BatchIdentification(
                    mod=name, provider="modio", matched=True, reason=""))
                continue
            if _cancelled():
                break
            resolved = modio_module.resolve_modio_meta(
                archive_path=None, staging_dir=folder, api_key=key,
                api_path=path, log_fn=_note)
            if resolved is not None and int(getattr(resolved, "mod_id", 0) or 0) > 0:
                if _cancelled():
                    break
                modio_module.write_modio_meta(meta_path, resolved)
                rows.append(BatchIdentification(
                    mod=name, provider="modio", matched=True, reason=""))
            else:
                rows.append(BatchIdentification(
                    mod=name, provider="modio", matched=False,
                    reason="no mod.io identity"))
            continue

        # Only report an existing Workshop stamp; there is no item lookup here.
        workshop_id = ""
        if parser.has_section("General"):
            workshop_id = (parser.get("General", "workshopItemId", fallback="")
                           or "").strip()
        if workshop_id:
            rows.append(BatchIdentification(
                mod=name, provider="workshop", matched=True, reason=""))
            continue

        if parser.has_section("ManagerPluginSource"):
            rows.append(BatchIdentification(
                mod=name, provider="plugin", matched=False,
                reason="manual link required"))
            continue
        if parser.has_section("General"):
            try:
                has_nexus = int(parser.get("General", "modid", fallback="0") or 0) > 0
            except (TypeError, ValueError):
                has_nexus = False
            if has_nexus:
                rows.append(BatchIdentification(
                    mod=name, provider="nexus", matched=False,
                    reason="paste a link"))
                continue

        rows.append(BatchIdentification(
            mod=name, provider="", matched=False,
            reason="no installed source identity"))

    return rows
