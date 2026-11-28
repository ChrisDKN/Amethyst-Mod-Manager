"""Manager-plugin download provider adapter for the manual source-link flow.

GUI-neutral: this only talks to the native amethyst_plugins download/repository
APIs and stamps the native ``[ManagerPluginSource]`` record.  A provider is
supported only when resolve() yields a non-empty lowercase 64-hex sha256;
``repositories.installed`` rejects records without it and that schema is not
relaxed here.
"""

from __future__ import annotations

import re
from contextlib import contextmanager
from pathlib import Path

from amethyst_plugins.dependencies import missing
from amethyst_plugins.downloads import providers, resolve, write_source
from amethyst_plugins.repositories import callback, installed, lookup
from Utils.app_log import app_log
from Utils.mods.source_link import LinkCandidate, SourceLinkError, VersionChoice

_CHECKSUM = re.compile(r"[0-9a-f]{64}")
_URI = re.compile(r"\b[a-z][a-z0-9+.-]*://[^\s'\"]+")
_CREDENTIAL = re.compile(r"(?i)\b(token|api[_-]?key|key|password|secret)\s*=\s*\S+")


def _sanitize_cause(exc: BaseException) -> str:
    """Keep the useful part of a native error without leaking URLs or tokens."""
    text = " ".join(str(exc).split())[:300]
    text = _URI.sub("<source>", text)
    return _CREDENTIAL.sub(r"\1=<redacted>", text)


@contextmanager
def _native_errors(code: str, message: str, details: dict):
    """Keep native provider failures behind the adapter's SourceLinkError contract.

    The native cause is included for diagnosis but URLs and credential values are
    redacted first; the raw exception remains available as ``__cause__``.
    """
    try:
        yield
    except SourceLinkError:
        raise
    except (ValueError, OSError) as exc:
        cause = _sanitize_cause(exc)
        raise SourceLinkError(
            code, f"{message}: {cause}" if cause else message, details) from exc


def _require_checksum(descriptor) -> None:
    value = descriptor.get("sha256", "") if isinstance(descriptor, dict) else ""
    if not isinstance(value, str) or not _CHECKSUM.fullmatch(value):
        raise SourceLinkError(
            "plugin_checksum_required",
            "This manager-plugin provider did not supply a usable SHA-256 checksum.",
            {"sha256": value},
        )


class PluginProviderAdapter:
    """Link a mod to a source handled by an installed manager plugin."""

    key = "plugin"

    def matches(self, url: str, game) -> bool:
        try:
            items, _errors = providers(getattr(game, "game_id", None), url)
        except (ValueError, OSError, TypeError, AttributeError):
            return False
        return bool(items)

    def parse(self, url: str, game, *, api=None) -> LinkCandidate:
        with _native_errors("plugin_provider_failed",
                            "Manager-plugin provider discovery failed.",
                            {"url": url}):
            items, _errors = providers(getattr(game, "game_id", None), url)
        if not items:
            raise SourceLinkError(
                "unsupported_source", "No manager plugin provider handles this source.",
                {"url": url})
        if len(items) != 1:
            raise SourceLinkError(
                "plugin_ambiguous",
                "More than one manager plugin provider handles this source.",
                {"url": url, "count": len(items)})
        item = items[0]
        with _native_errors("plugin_resolve_failed",
                            "The manager-plugin provider could not resolve this source.",
                            {"plugin_id": item["plugin_id"],
                             "provider_id": item["provider_id"], "source": url}):
            plan = resolve(game, item["plugin_id"], item["provider_id"], url, app_log, None)
        descriptor = plan.descriptor
        _require_checksum(descriptor)
        metadata = descriptor.get("metadata") or {}
        return LinkCandidate(
            provider=self.key,
            url=url,
            name=str(metadata.get("name", "") or ""),
            identity={
                "plugin_id": item["plugin_id"],
                "provider_id": item["provider_id"],
                "source": url,
                "identity": descriptor["identity"],
                "sha256": descriptor["sha256"],
            },
            details={
                "game": game,
                "descriptor": descriptor,
                "provider_label": item.get("label", ""),
                "repository": bool(item.get("repository")),
            },
        )

    def versions(self, candidate: LinkCandidate, game, *, api=None):
        descriptor = candidate.details["descriptor"]
        exact = VersionChoice(
            id=candidate.identity["identity"],
            label=candidate.url,
            version=str((descriptor.get("metadata") or {}).get("version", "") or ""),
            recommended=True,
            payload={"source": candidate.identity["source"], "descriptor": descriptor},
        )
        if not candidate.details.get("repository"):
            return [exact]
        try:
            plugin, provider = lookup(game.game_id, candidate.identity["plugin_id"],
                                      candidate.identity["provider_id"])
            # get_versions only needs the version catalogue. The synthetic
            # record deliberately has no identity/checksum/size so the plugin
            # lists every version instead of only upgrades to the parsed one.
            synthetic = {
                "version": 1,
                "plugin_id": candidate.identity["plugin_id"],
                "provider_id": candidate.identity["provider_id"],
                "provider_label": candidate.details.get("provider_label", ""),
                "identity": "",
                "size_bytes": 0,
                "sha256": "",
                "metadata": {},
            }
            result = callback(game, plugin, provider, "get_versions",
                              {"installed": synthetic}, app_log, None)
            versions = result.get("versions") if isinstance(result, dict) else None
            if not isinstance(versions, list):
                return [exact]
            choices = [
                VersionChoice(
                    id=str(row["id"]),
                    label=str(row.get("label") or row["version"]),
                    version=str(row["version"]),
                    recommended=(row["id"] == result.get("recommended")),
                    payload={"source": row["source"], "descriptor": None},
                )
                for row in versions
                if isinstance(row, dict)
                and all(isinstance(row.get(key), str) and row[key]
                        for key in ("id", "version", "source"))
            ]
            return choices or [exact]
        except Exception as exc:
            # repositories.installed rejects a record without a 64-hex sha256,
            # so a plugin that validates its installed record can raise here.
            # The exact source is fully resolved and always a safe fallback.
            app_log(f"source_link: manager-plugin version catalogue unavailable: {exc}")
            return [exact]

    def stamp(self, meta_path: Path, candidate: LinkCandidate,
              choice: VersionChoice, *, confirm_replace: bool = False) -> None:
        descriptor = choice.payload.get("descriptor")
        if not isinstance(descriptor, dict):
            identity = candidate.identity
            with _native_errors("plugin_resolve_failed",
                                "The manager-plugin provider could not resolve the selected source.",
                                {"plugin_id": identity["plugin_id"],
                                 "provider_id": identity["provider_id"],
                                 "source": choice.payload["source"]}):
                plan = resolve(candidate.details.get("game"), identity["plugin_id"],
                               identity["provider_id"], choice.payload["source"],
                               app_log, None)
                descriptor = plan.descriptor
                _require_checksum(descriptor)
        record = {
            "version": 1,
            "plugin_id": candidate.identity["plugin_id"],
            "provider_id": candidate.identity["provider_id"],
            "provider_label": candidate.details.get("provider_label", ""),
            "identity": descriptor["identity"],
            "size_bytes": descriptor["size_bytes"],
            "sha256": descriptor["sha256"],
            "metadata": descriptor["metadata"],
        }
        meta_path = Path(meta_path)
        with _native_errors("plugin_metadata_invalid",
                            "The existing manager-plugin metadata is malformed.",
                            {"plugin_id": record["plugin_id"],
                             "provider_id": record["provider_id"],
                             "source": candidate.identity["source"]}):
            existing = installed(meta_path)
        keys = ("plugin_id", "provider_id", "identity")
        new_key = tuple(record[key] for key in keys)
        if existing is not None:
            old_key = tuple(existing[key] for key in keys)
            if old_key != new_key and not confirm_replace:
                raise SourceLinkError(
                    "source_conflict",
                    "This mod is already linked to a different manager-plugin source.",
                    {"existing": dict(zip(keys, old_key)),
                     "new": dict(zip(keys, new_key))})
        write_source(meta_path, record)

    def verify(self, mod_dir: Path, candidate: LinkCandidate,
               choice: VersionChoice, *, game=None, api=None) -> tuple[str, str]:
        with _native_errors("plugin_metadata_invalid",
                            "The existing manager-plugin metadata is malformed.",
                            {"plugin_id": candidate.identity["plugin_id"],
                             "provider_id": candidate.identity["provider_id"],
                             "source": candidate.identity["source"]}):
            record = installed(Path(mod_dir) / "meta.ini")
        if record is None:
            return ("none", "")
        identity = candidate.identity
        keys = ("plugin_id", "provider_id", "identity")
        if tuple(record[key] for key in keys) == tuple(identity[key] for key in keys):
            return ("metadata", str((record.get("metadata") or {}).get("version", "") or ""))
        return ("none", "")

    def followup(self, game, profile: Path, mod_name: str,
                 candidate: LinkCandidate, choice: VersionChoice, *, api=None, log=None):
        with _native_errors("plugin_dependency_failed",
                            "Manager-plugin dependency lookup failed.",
                            {"plugin_id": candidate.identity["plugin_id"],
                             "provider_id": candidate.identity["provider_id"],
                             "source": candidate.identity["source"]}):
            absent = missing(game, profile, [{
                "plugin_id": candidate.identity["plugin_id"],
                "provider_id": candidate.identity["provider_id"],
                "source": candidate.identity["source"],
            }], log or app_log)
        return ((), tuple(absent))
