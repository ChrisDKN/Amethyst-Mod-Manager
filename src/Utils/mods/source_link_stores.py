"""Provider adapters for the manual source-link flow.

GUI-neutral: each adapter talks to its provider through an injected API and
reads/writes the provider's native ``meta.ini`` data via the existing metadata
modules.  The link itself is the identity - display/folder names are never used
to decide which mod a link refers to.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from Nexus.nexus_meta import normalise_game_domain, read_meta, write_meta
from Thunderstore.repositories import make_link, version_key
from Utils.bg3.modio_loader import load_modio_module
from Utils.games.workshop import supports_workshop_install, workshop_app_id
from Utils.mods.metadata import meta_file_lock
from Utils.wizards.workshop import parse_item_id
from Thunderstore.thunderstore_api import fetch_versions
from Thunderstore.thunderstore_identify import read_manifest
from Thunderstore.thunderstore_meta import (
    ThunderstoreModMeta, read_meta as read_thunderstore_meta,
    write_meta as write_thunderstore_meta,
)
from Thunderstore.thunderstore_requirements import (
    filter_already_installed, resolve_dependencies,
)
from Utils.app_log import app_log
from Utils.mods.source_link import LinkCandidate, SourceLinkError, VersionChoice
from Utils.mods.versions import sort_key

# BG3's mod.io modules live under a path with spaces, so they are loaded by
# file path through the loader rather than imported directly.
_modio_meta = load_modio_module("modio_meta")
read_modio_meta = _modio_meta.read_modio_meta
write_modio_meta = _modio_meta.write_modio_meta
read_publish_metadata_from_staging = _modio_meta.read_publish_metadata_from_staging

_NEXUS_HOSTS = {"nexusmods.com", "www.nexusmods.com"}
_NEXUS_MOD_PATH = re.compile(r"^/([^/]+)/mods/(\d+)/?$")

_TS_HOSTS = {"thunderstore.io"}
_TS_PATH = re.compile(
    r"^/c/(?P<community>[^/]+)/p/(?P<namespace>[^/]+)/(?P<name>[^/]+)"
    r"(?:/v/(?P<version>[^/]+))?/?$")
_HEX_HOST_SUFFIX = ".hexium.gg"
_HEX_PATH = re.compile(
    r"^/mods/(?P<namespace>[^/]+)/(?P<name>[^/]+)"
    r"(?:/(?:v/)?(?P<version>[^/]+))?/?$")

_MODIO_HOSTS = {"mod.io"}
_MODIO_PATH = re.compile(r"^/g/baldursgate3/m/(?P<slug>[^/]+)/?$")

_WORKSHOP_HOSTS = {"steamcommunity.com", "www.steamcommunity.com"}
_WORKSHOP_PATHS = {"/sharedfiles/filedetails", "/workshop/filedetails"}


def _is_http_url(url: str) -> bool:
    """Public provider links are HTTP(S); plugin source URIs are handled separately."""
    return urlparse(url).scheme.lower() in ("http", "https")


def _link_match(url: str, source: str):
    """Return (path match, community) for a Thunderstore-family URL."""
    parsed = urlparse(url)
    if not _is_http_url(url):
        return None
    host = (parsed.hostname or "").lower()
    if source == "thunderstore":
        if host not in _TS_HOSTS:
            return None
        match = _TS_PATH.match(parsed.path)
        return (match, match.group("community")) if match else None
    if host.endswith(_HEX_HOST_SUFFIX):
        match = _HEX_PATH.match(parsed.path)
        community = host[:-len(_HEX_HOST_SUFFIX)]
        if match and community:
            return match, community
    return None


def _exact_file_id(candidate, choice) -> int:
    """The file being stamped - the chosen file, not merely the URL's file_id."""
    return int(choice.payload.get("file_id")
               or candidate.identity.get("file_id")
               or 0)


class NexusAdapter:
    """Link a mod to a Nexus mod page, optionally pinning one exact file."""

    key = "nexus"

    def matches(self, url: str, game) -> bool:
        parsed = urlparse(url)
        if not _is_http_url(url) or (parsed.hostname or "").lower() not in _NEXUS_HOSTS:
            return False
        return _NEXUS_MOD_PATH.match(parsed.path) is not None

    def parse(self, url: str, game, *, api=None) -> LinkCandidate:
        parsed = urlparse(url)
        match = _NEXUS_MOD_PATH.match(parsed.path) if self.matches(url, game) else None
        if match is None:
            raise SourceLinkError(
                "unsupported_source", "That is not a Nexus mod page link.",
                {"url": url})
        domain = normalise_game_domain(match.group(1))
        mod_id_text = match.group(2)
        game_domains = {
            normalise_game_domain(d)
            for d in (getattr(game, "nexus_game_domains", ()) or ())
        }
        if domain not in game_domains:
            raise SourceLinkError(
                "wrong_game",
                f"That Nexus link is for '{domain}', which is not a domain of this game.",
                {"url": url, "domain": domain})
        if api is None:
            raise SourceLinkError(
                "api_unavailable",
                "Sign in to Nexus to look up this mod page.",
                {"url": url})

        mod_id = int(mod_id_text)
        try:
            file_id = int(parse_qs(parsed.query).get("file_id", [0])[0])
        except (TypeError, ValueError):
            file_id = 0
        info = api.get_mod(domain, mod_id)
        return LinkCandidate(
            provider="nexus",
            url=f"https://www.nexusmods.com/{domain}/mods/{mod_id}",
            name=getattr(info, "name", "") or "",
            identity={
                "domain": domain,
                "mod_id": mod_id,
                "file_id": file_id or None,
            },
            details={"mod": info},
        )

    def versions(self, candidate: LinkCandidate, game, *, api=None):
        if api is None:
            raise SourceLinkError(
                "api_unavailable", "Sign in to Nexus to list this mod's files.",
                {"url": candidate.url})
        identity = candidate.identity
        domain = identity["domain"]
        files = api.get_mod_files(domain, identity["mod_id"]).files
        exact_file = identity.get("file_id")
        # sort_key consumes raw file objects (category_name/uploaded_timestamp);
        # VersionChoice only carries the derived values in its payload.
        return [
            VersionChoice(
                id=str(f.file_id),
                label=f"{f.name or ''} ({f.version or ''})",
                version=f.version or f.mod_version or "",
                recommended=(f.file_id == exact_file),
                payload={
                    "file_id": f.file_id,
                    "version": f.version or f.mod_version or "",
                    "file_name": f.file_name or "",
                    "name": f.name or "",
                    "category": f.category_name or "",
                    "uploaded": f.uploaded_timestamp or 0,
                },
            )
            for f in sorted(files, key=sort_key)
        ]

    def stamp(self, meta_path: Path, candidate: LinkCandidate,
              choice: VersionChoice, *, confirm_replace: bool = False) -> None:
        meta_path = Path(meta_path)
        meta = read_meta(meta_path)
        identity = candidate.identity
        domain = normalise_game_domain(identity["domain"])
        mod_id = int(identity["mod_id"])
        file_id = _exact_file_id(candidate, choice)
        existing_domain = normalise_game_domain(meta.game_domain)
        # A file_id of 0 means the existing identity pins no file, so only a
        # populated file_id can conflict with the one being stamped.
        conflicts = (
            (meta.mod_id or meta.file_id)
            and (meta.mod_id != mod_id
                 or (meta.file_id and meta.file_id != file_id)
                 or (existing_domain and existing_domain != domain))
        )
        if conflicts and not confirm_replace:
            raise SourceLinkError(
                "source_conflict",
                "This mod is already linked to a different Nexus file.",
                {"existing": {"domain": existing_domain, "mod_id": meta.mod_id,
                              "file_id": meta.file_id},
                 "new": {"domain": domain, "mod_id": mod_id, "file_id": file_id}})

        info = candidate.details.get("mod")
        meta.game_domain = domain
        meta.mod_id = mod_id
        meta.file_id = file_id
        # An exact file without a version string must not wipe a known version.
        if choice.version:
            meta.version = choice.version
        meta.nexus_name = getattr(info, "name", "") or ""
        meta.description = getattr(info, "summary", "") or ""
        meta.author = getattr(info, "author", "") or ""
        meta.uploaded_by = getattr(info, "uploaded_by", "") or ""
        meta.category_id = getattr(info, "category_id", 0) or 0
        meta.category_name = getattr(info, "category_name", "") or ""
        meta.nexus_url = f"https://www.nexusmods.com/{domain}/mods/{mod_id}"
        if not meta.installed:
            meta.installed = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
        write_meta(meta_path, meta)

    def verify(self, mod_dir: Path, candidate: LinkCandidate,
               choice: VersionChoice, *, game=None, api=None) -> tuple[str, str]:
        meta = read_meta(Path(mod_dir) / "meta.ini")
        version = meta.version or ""
        identity = candidate.identity
        domain = normalise_game_domain(identity["domain"])
        mod_id = int(identity["mod_id"])
        exact_file = _exact_file_id(candidate, choice)

        if meta.mod_id and meta.file_id:
            if (normalise_game_domain(meta.game_domain) == domain
                    and meta.mod_id == mod_id
                    and (not exact_file or meta.file_id == exact_file)):
                return ("metadata", version)
            return ("none", version)

        if meta.installation_file and game is not None and api is not None:
            from Nexus.archive_lookup import find_archive_pages
            from Utils.downloads.core import scan_download_dirs

            domains = tuple(getattr(game, "nexus_game_domains", ()) or ()) or (domain,)
            try:
                for entry in scan_download_dirs(getattr(game, "name", None)):
                    archive = getattr(entry, "path", None)
                    if archive is None or archive.name != meta.installation_file:
                        continue
                    for page in find_archive_pages(archive, domains, api):
                        if (page.get("mod_id") == mod_id
                                and normalise_game_domain(page.get("domain", ""))
                                == normalise_game_domain(domain)):
                            return ("archive-hash", version)
            except Exception:
                pass  # best-effort: a missing/unreadable archive is not a match
        return ("none", version)

    def followup(self, game, profile: Path, mod_name: str,
                 candidate: LinkCandidate, choice: VersionChoice, *, api=None, log=None):
        from Utils.mods.install import check_nexus_flags_after_install

        check_nexus_flags_after_install(
            game, mod_name, log or (lambda message: None), profile_dir=profile)
        return ((), ())


class ThunderstoreAdapter:
    """Link a mod to a Thunderstore or Hexium package page.

    The package identity (source/community/namespace/name) comes from the URL;
    the manifest and the native meta.ini are the only sources of version truth.
    """

    def __init__(self, source: str = "thunderstore"):
        if source not in ("thunderstore", "hexium"):
            raise SourceLinkError(
                "unsupported_source", f"Unknown Thunderstore source {source!r}.",
                {"source": source})
        self.source = source
        self.key = source

    def matches(self, url: str, game) -> bool:
        return _link_match(url, self.source) is not None

    def parse(self, url: str, game, *, api=None) -> LinkCandidate:
        found = _link_match(url, self.source)
        if found is None:
            raise SourceLinkError(
                "unsupported_source", f"That is not a {self.source} mod page link.",
                {"url": url})
        match, community = found
        expected = (getattr(game, f"{self.source}_community", "") or "").lower()
        if community.lower() != expected:
            raise SourceLinkError(
                "wrong_game",
                f"That {self.source} link is for community '{community}', "
                "which is not this game's community.",
                {"url": url, "community": community})
        namespace, name = match.group("namespace"), match.group("name")
        version = match.group("version") or None
        if self.source == "thunderstore":
            canonical = f"https://thunderstore.io/c/{community}/p/{namespace}/{name}/"
            if version:
                canonical += f"v/{version}/"
        else:
            canonical = f"https://{community}.hexium.gg/mods/{namespace}/{name}"
            if version:
                canonical += f"/v/{version}"
        return LinkCandidate(
            provider=self.source,
            url=canonical,
            name=name,
            identity={
                "community": community,
                "namespace": namespace,
                "name": name,
                "version": version,
                "source": self.source,
            },
            details={},
        )

    def versions(self, candidate: LinkCandidate, game, *, api=None):
        identity = candidate.identity
        namespace, name = identity["namespace"], identity["name"]
        exact = identity.get("version")
        rows = [
            row for row in fetch_versions(namespace, name, source=self.source,
                                          community=identity["community"])
            if isinstance(row, dict) and str(row.get("version_number") or "")
        ]
        # The API does not guarantee order; newest first via the native parser
        # (1001 must sort above 999), then recommend the pinned version if any.
        rows.sort(key=lambda row: version_key(row["version_number"]), reverse=True)
        return [
            VersionChoice(
                id=f"{namespace}-{name}-{version}",
                label=version,
                version=version,
                recommended=(version == exact) if exact else (index == 0),
                payload={"version_info": row},
            )
            for index, row in enumerate(rows)
            for version in (str(row["version_number"]),)
        ]

    def stamp(self, meta_path: Path, candidate: LinkCandidate,
              choice: VersionChoice, *, confirm_replace: bool = False) -> None:
        meta_path = Path(meta_path)
        existing = read_thunderstore_meta(meta_path)
        identity = candidate.identity
        community, namespace = identity["community"], identity["namespace"]
        name, version = identity["name"], choice.version
        conflicts = bool(existing.package_id) and (
            (existing.source and existing.source != self.source)
            or (existing.community and existing.community != community)
            or existing.namespace != namespace
            or existing.name != name
        )
        if conflicts and not confirm_replace:
            raise SourceLinkError(
                "source_conflict",
                "This mod is already linked to a different Thunderstore package.",
                {"existing": {"source": existing.source,
                              "community": existing.community,
                              "namespace": existing.namespace,
                              "name": existing.name},
                 "new": {"source": self.source, "community": community,
                         "namespace": namespace, "name": name}})

        info = choice.payload.get("version_info") or {}
        if not isinstance(info, dict):
            info = {}
        deps = info.get("dependencies")
        dependencies = (", ".join(str(d) for d in deps if d)
                        if isinstance(deps, (list, tuple)) else str(deps or ""))
        try:
            file_size = int(info.get("file_size") or info.get("size") or 0)
        except (TypeError, ValueError):
            file_size = 0
        download_url = info.get("download_url") or ""
        if not download_url and self.source == "thunderstore":
            download_url = (f"https://thunderstore.io/package/download/"
                            f"{namespace}/{name}/{version}/")
        meta = ThunderstoreModMeta(
            source=self.source,
            community=community,
            namespace=namespace,
            name=name,
            version=version,
            full_name=f"{namespace}-{name}-{version}",
            download_url=download_url,
            website_url=info.get("website_url") or "",
            description=info.get("description") or "",
            icon_url=info.get("icon_url") or info.get("icon") or "",
            file_size=file_size,
            dependencies=dependencies,
            installed=existing.installed or datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        )
        # Ignoring updates is user state, not part of the link we are stamping.
        if not conflicts and existing.package_id:
            for field in ("latest_version", "has_update", "ignore_update",
                          "ignored_version", "is_deprecated"):
                setattr(meta, field, getattr(existing, field))
        try:
            write_thunderstore_meta(
                meta_path, meta, replace_source=(existing.source != self.source))
        except ValueError as exc:
            if "Changing repository source" not in str(exc):
                raise
            raise SourceLinkError(
                "source_conflict",
                "This mod is linked to a different mod source; confirm the replacement.",
                {"existing": existing.source, "new": self.source}) from exc

    def verify(self, mod_dir: Path, candidate: LinkCandidate,
               choice: VersionChoice, *, game=None, api=None) -> tuple[str, str]:
        manifest = read_manifest(Path(mod_dir))
        version = manifest.version if manifest else ""
        if (manifest and manifest.name == candidate.identity["name"]
                and version == choice.version):
            return ("manifest", version)
        return ("none", version)

    def followup(self, game, profile: Path, mod_name: str,
                 candidate: LinkCandidate, choice: VersionChoice, *, api=None, log=None):
        from Utils.mods.copy import resolve_target_staging

        identity = candidate.identity
        community = identity["community"]
        link = make_link(identity["namespace"], identity["name"], choice.version,
                         community, source=self.source)
        graph = resolve_dependencies(link, community=community)
        if graph.failed:
            (log or app_log)(
                f"source_link: unresolved {self.source} packages: "
                f"{', '.join(str(item) for item in graph.failed)}")
        try:
            staging = Path(resolve_target_staging(game, profile))
        except AttributeError:
            # GUI-neutral callers/stubs need not implement game staging paths.
            staging = Path(profile) / "mods"
        todo, _present = filter_already_installed(
            graph.packages, staging, keep_root=False)
        missing = tuple(p for p in todo if not getattr(p, "is_root", False))
        return missing, ()


def _versions_compatible(expected: str, actual: str) -> bool:
    """Compare dotted numeric versions by their shared leading components.

    mod.io file versions (``2.0.0``) are usually shorter than Larian's decoded
    ``Version64`` (``2.0.0.0`` / ``2.0.0.1234``), so exact string equality would
    reject legitimate installs. Unparseable input falls back to a mod-id-only
    match, decided by the caller.
    """
    def parts(raw: str) -> tuple[int, ...] | None:
        text = (raw or "").strip()
        chunks = text.split(".")
        if not text or any(not chunk.isdigit() for chunk in chunks):
            return None
        return tuple(int(chunk) for chunk in chunks)

    left, right = parts(expected), parts(actual)
    if left is None or right is None:
        return True
    shared = min(len(left), len(right))
    return left[:shared] == right[:shared]


class ModioAdapter:
    """Link a Baldur's Gate 3 mod to its mod.io page; mod_id is the identity."""

    key = "modio"

    def matches(self, url: str, game) -> bool:
        parsed = urlparse(url)
        return (_is_http_url(url)
                and (parsed.hostname or "").lower() in _MODIO_HOSTS
                and _MODIO_PATH.match(parsed.path) is not None)

    def _api(self, api=None):
        if api is not None:
            return api
        key, path = load_modio_module("modio_key").load_modio_credentials()
        if not (key and path):
            raise SourceLinkError(
                "modio_credentials",
                "Enter your mod.io API key and API path to link a mod.io mod.",
                {})
        return load_modio_module("modio_api").ModioAPI(key, path)

    def parse(self, url: str, game, *, api=None) -> LinkCandidate:
        parsed = urlparse(url)
        match = _MODIO_PATH.match(parsed.path) if self.matches(url, game) else None
        if match is None:
            raise SourceLinkError(
                "unsupported_source", "That is not a BG3 mod.io mod page link.",
                {"url": url})
        if getattr(game, "game_id", None) != "baldurs_gate_3":
            raise SourceLinkError(
                "unsupported_game",
                "mod.io source links are only supported for Baldur's Gate 3.",
                {"url": url, "game_id": getattr(game, "game_id", None)})

        api = self._api(api)
        slug = match.group("slug")
        result = api.search_mods(query=slug, limit=50)
        rows = result[0] if isinstance(result, tuple) else result
        wanted = f"/m/{slug}".lower()
        found = [
            row for row in rows
            if urlparse(getattr(row, "profile_url", "") or "").path.rstrip("/")
            .lower().endswith(wanted)
        ]
        if not found:
            raise SourceLinkError(
                "modio_mod_not_found",
                f"No BG3 mod.io mod with the slug '{slug}' was found.",
                {"url": url, "slug": slug})
        if len(found) > 1:
            raise SourceLinkError(
                "modio_ambiguous",
                f"More than one mod.io mod matches the slug '{slug}'.",
                {"url": url, "slug": slug})
        row = found[0]
        profile_url = row.profile_url
        return LinkCandidate(
            provider="modio",
            url=profile_url,
            name=getattr(row, "name", "") or "",
            identity={
                "mod_id": int(row.mod_id),
                "slug": slug,
                "profile_url": profile_url,
            },
            details={"mod": row, "api": api},
        )

    def versions(self, candidate: LinkCandidate, game, *, api=None):
        api = api or candidate.details.get("api") or self._api()
        files = api.get_mod_files(int(candidate.identity["mod_id"]))
        return [
            VersionChoice(
                id=str(file.file_id),
                label=f"{file.version} · {file.filename}",
                version=file.version or "",
                recommended=(index == 0),
                payload={
                    "file_id": file.file_id,
                    "version": file.version or "",
                    "name": file.filename or "",
                },
            )
            for index, file in enumerate(files)
        ]

    def stamp(self, meta_path: Path, candidate: LinkCandidate,
              choice: VersionChoice, *, confirm_replace: bool = False) -> None:
        meta_path = Path(meta_path)
        with meta_file_lock(meta_path):
            meta = read_modio_meta(meta_path)
            mod_id = int(candidate.identity["mod_id"])
            replacing = bool(meta.mod_id and meta.mod_id != mod_id)
            if replacing and not confirm_replace:
                raise SourceLinkError(
                    "source_conflict",
                    "This mod is already linked to a different mod.io mod.",
                    {"existing": {"mod_id": meta.mod_id, "file_id": meta.file_id},
                     "new": {"mod_id": mod_id,
                             "file_id": choice.payload.get("file_id", 0)}})

            file_id = int(choice.payload.get("file_id") or 0)
            version = choice.payload.get("version") or ""
            summary = candidate.details.get("mod")
            meta.mod_id = mod_id
            meta.file_id = file_id
            meta.version = version
            meta.name = choice.payload.get("name") or ""
            meta.profile_url = candidate.identity.get("profile_url") or ""
            meta.latest_file_id = int(getattr(summary, "latest_file_id", 0) or 0) or file_id
            meta.latest_version = getattr(summary, "latest_version", "") or version
            if replacing:
                # Update state belongs to the previous mod, not the new identity.
                meta.has_update = False
            if not meta.installed:
                meta.installed = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
            write_modio_meta(meta_path, meta)

    def verify(self, mod_dir: Path, candidate: LinkCandidate,
               choice: VersionChoice, *, game=None, api=None) -> tuple[str, str]:
        mod_id, _name, decoded_version, _pak = read_publish_metadata_from_staging(
            Path(mod_dir))
        version = decoded_version or ""
        expected = choice.version or ""
        if (mod_id == int(candidate.identity["mod_id"])
                and _versions_compatible(expected, version)):
            return ("metadata", version)
        return ("none", version)

    def followup(self, game, profile: Path, mod_name: str,
                 candidate: LinkCandidate, choice: VersionChoice, *, api=None, log=None):
        return ((), ())


class WorkshopAdapter:
    """Link a mod to a Steam Workshop item; app_id + item_id are the identity.

    There is no item-details API in the tree, so no title/updated lookup is
    attempted - only the game's store App ID and the parsed item ID are stamped.
    """

    key = "workshop"

    def matches(self, url: str, game) -> bool:
        parsed = urlparse(url)
        return (_is_http_url(url)
                and (parsed.hostname or "").lower() in _WORKSHOP_HOSTS
                and parsed.path.rstrip("/") in _WORKSHOP_PATHS)

    def parse(self, url: str, game, *, api=None) -> LinkCandidate:
        try:
            item_id = parse_item_id(url)
        except ValueError as exc:
            raise SourceLinkError("invalid_item_id", str(exc), {"url": url}) from exc
        app_id = workshop_app_id(game)
        if not supports_workshop_install(game):
            raise SourceLinkError(
                "unsupported_game",
                "Steam Workshop source links are not supported for this game.",
                {"url": url, "app_id": app_id})
        return LinkCandidate(
            provider="workshop",
            url=f"https://steamcommunity.com/sharedfiles/filedetails/?id={item_id}",
            name="",
            identity={"app_id": app_id, "item_id": item_id},
            details={},
        )

    def versions(self, candidate: LinkCandidate, game, *, api=None):
        return [VersionChoice(
            id=candidate.identity["item_id"],
            label=candidate.url,
            version="",
            recommended=True,
            payload={},
        )]

    def stamp(self, meta_path: Path, candidate: LinkCandidate,
              choice: VersionChoice, *, confirm_replace: bool = False) -> None:
        meta_path = Path(meta_path)
        with meta_file_lock(meta_path):
            meta = read_meta(meta_path)
            identity = candidate.identity
            item_id = identity["item_id"]
            if (meta.workshop_item_id and meta.workshop_item_id != item_id
                    and not confirm_replace):
                raise SourceLinkError(
                    "source_conflict",
                    "This mod is already linked to a different Workshop item.",
                    {"existing": {"app_id": meta.workshop_app_id,
                                  "item_id": meta.workshop_item_id},
                     "new": {"app_id": identity["app_id"], "item_id": item_id}})
            meta.workshop_app_id = identity["app_id"]
            meta.workshop_item_id = item_id
            if not meta.installed:
                meta.installed = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
            write_meta(meta_path, meta)

    def verify(self, mod_dir: Path, candidate: LinkCandidate,
               choice: VersionChoice, *, game=None, api=None) -> tuple[str, str]:
        meta = read_meta(Path(mod_dir) / "meta.ini")
        if meta.workshop_item_id == candidate.identity["item_id"]:
            return ("metadata", "")
        return ("none", "")

    def followup(self, game, profile: Path, mod_name: str,
                 candidate: LinkCandidate, choice: VersionChoice, *, api=None, log=None):
        return ((), ())
