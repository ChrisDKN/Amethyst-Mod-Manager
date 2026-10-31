from __future__ import annotations

from dataclasses import dataclass
import re
import threading
import time
from urllib.parse import quote, urlsplit


SOURCES = ("thunderstore", "hexium")
_VERSION = re.compile(r"^(\d+)\.(\d+)\.(\d+)(?:-(alpha|beta|rc)\.(\d+))?$")
_cache = {}
_lock = threading.RLock()


def version_key(value):
    match = _VERSION.fullmatch(str(value))
    if match is None:
        return (-1, -1, -1, -1, -1)
    major, minor, patch, stage, number = match.groups()
    return (int(major), int(minor), int(patch),
            {"alpha": 0, "beta": 1, "rc": 2, None: 3}[stage], int(number or 0))


def split_full_name(value):
    match = re.fullmatch(r"(.+)-(\d+\.\d+\.\d+(?:-(?:alpha|beta|rc)\.\d+)?)", str(value))
    if match is None or "-" not in match[1]:
        return "", "", ""
    namespace, name = match[1].rsplit("-", 1)
    if any(re.fullmatch(r"[A-Za-z0-9_-]+", part) is None for part in (namespace, name)):
        return "", "", ""
    return namespace, name, match[2]


def source_of(value):
    return getattr(value, "source", "hexium" if getattr(value, "host", "") == "hexium.gg" else "thunderstore")


def include_prereleases():
    from Utils.ui.config import load_thunderstore_flag
    return load_thunderstore_flag("hexium_include_prereleases")


def set_include_prereleases(enabled):
    from Utils.ui.config import save_thunderstore_flag
    save_thunderstore_flag("hexium_include_prereleases", bool(enabled))


def validate_community(community):
    if not isinstance(community, str) or re.fullmatch(r"[a-z0-9](?:[a-z0-9_-]{0,61}[a-z0-9])?", community) is None:
        raise ValueError("Invalid repository community")
    return community


def catalogue(community="", *, force=False, source="hexium"):
    from Thunderstore.thunderstore_download import fetch_json
    validate_community(community)
    if source not in SOURCES:
        raise ValueError("Unknown repository source")
    host = "hexium.gg" if source == "hexium" else "thunderstore.io"
    key = (source, community)
    with _lock:
        cached = _cache.get(key)
        if not force and cached and time.monotonic() - cached[0] < 300:
            return cached[1]
        rows = fetch_json(f"https://{host}/c/{community}/api/v1/package/", timeout=60)
        if not isinstance(rows, list) or any(not isinstance(p, dict) or not isinstance(p.get("versions"), list) for p in rows):
            raise ValueError(f"Could not load the {source} catalogue")
        packages = {}
        for raw in rows:
            namespace, name = raw.get("owner"), raw.get("name")
            if not isinstance(namespace, str) or not isinstance(name, str):
                raise ValueError("Invalid repository package identity")
            versions = []
            for item in raw["versions"]:
                if not isinstance(item, dict) or version_key(item.get("version_number"))[0] < 0:
                    raise ValueError("Invalid repository version")
                if type(item.get("file_size", 0)) is not int or item.get("file_size", 0) < 0:
                    raise ValueError("Invalid repository archive size")
                versions.append({**item, "namespace": namespace, "name": name,
                                 "full_name": f"{namespace}-{name}-{item['version_number']}",
                                 "size": item.get("file_size", 0),
                                 "full_version_name": f"{namespace}-{name}-{item['version_number']}",
                                 "icon_url": item.get("icon", ""),
                                 "datetime_created": item.get("date_created", ""),
                                 "download_count": item.get("downloads", 0),
                                 "dependency_count": len(item.get("dependencies") or [])})
            packages[f"{namespace}-{name}"] = {**raw, "versions": sorted(versions, key=lambda v: version_key(v["version_number"]), reverse=True)}
        _cache[key] = (time.monotonic(), packages)
        return packages


def versions(namespace, name, community="", *, force=False):
    return list(catalogue(community, force=force).get(f"{namespace}-{name}", {}).get("versions", []))


def latest(namespace, name, community="", *, prereleases=None):
    allow = include_prereleases() if prereleases is None else prereleases
    return next((v["version_number"] for v in versions(namespace, name, community)
                 if v.get("is_active", True) and (allow or "-" not in v["version_number"])), "")


def version_info(namespace, name, version, community="", *, force=False):
    return next((dict(v) for v in versions(namespace, name, community, force=force)
                 if v["version_number"] == version and v.get("is_active", True)), None)


@dataclass
class RepositoryLink:
    namespace: str
    name: str
    version: str
    community: str = ""
    source: str = "hexium"
    raw: str = ""
    download_override: str = ""
    thunderstore_community: str | None = None

    def __post_init__(self):
        if self.source != "hexium":
            raise ValueError("Unsupported repository source")
        if self.community:
            validate_community(self.community)
        if (re.fullmatch(r"[A-Za-z0-9_-]+", self.namespace) is None
                or re.fullmatch(r"[A-Za-z0-9_]+", self.name) is None
                or version_key(self.version)[0] < 0):
            raise ValueError("Invalid Hexium package identity")

    @property
    def host(self):
        return "hexium.gg"

    @property
    def package_id(self):
        return f"{self.namespace}-{self.name}"

    @property
    def full_name(self):
        return f"{self.package_id}-{self.version}"

    @property
    def archive_name(self):
        return f"hexium-{self.full_name}.zip"

    @property
    def download_url(self):
        validate_community(self.community)
        url = self.download_override or (version_info(self.namespace, self.name, self.version, self.community) or {}).get("download_url", "")
        parsed = urlsplit(url)
        if parsed.scheme != "https" or parsed.hostname not in {"hexium.gg", "cdn.hexium.gg", f"{self.community}.hexium.gg"} or parsed.username or parsed.password:
            raise ValueError("Invalid Hexium download URL")
        return url

    @property
    def api_url(self):
        return f"https://hexium.gg/api/experimental/package/{quote(self.namespace)}/{quote(self.name)}/"


def make_link(namespace, name, version, community="", source="thunderstore", download_url=""):
    if source == "hexium":
        return RepositoryLink(namespace, name, version, community, download_override=download_url)
    if source != "thunderstore":
        raise ValueError("Unknown package source")
    from Thunderstore.ror2mm_handler import Ror2mmLink
    return Ror2mmLink(namespace=namespace, name=name, version=version, community=community)


def package_url(community, namespace, name, source="hexium"):
    if source == "hexium":
        validate_community(community)
        return f"https://{community}.hexium.gg/mods/{quote(namespace, safe='')}/{quote(name, safe='')}"
    from Thunderstore.thunderstore_api import package_url as thunderstore_url
    return thunderstore_url(community, namespace, name)


def fetch_filters(community):
    from Thunderstore.thunderstore_api import ThunderstoreFilters
    names = sorted({c for p in catalogue(community).values() for c in p.get("categories", [])})
    return ThunderstoreFilters(categories=[(n, n, n) for n in names])


def fetch_listing(community, *, page=0, query="", ordering="", included_categories=None,
                  excluded_categories=None, deprecated=False, nsfw=False, force=False, **_kw):
    from Thunderstore.thunderstore_api import ThunderstoreListing, PAGE_SIZE
    allow_pre = include_prereleases()
    rows = []
    for raw in catalogue(community, force=force).values():
        candidates = [v for v in raw["versions"] if v.get("is_active", True)
                      and (allow_pre or "-" not in v["version_number"])]
        if not candidates:
            continue
        info = candidates[0]
        categories = set(raw.get("categories") or [])
        if (not deprecated and raw.get("is_deprecated") or not nsfw and raw.get("has_nsfw_content")
                or set(included_categories or []) - categories or set(excluded_categories or []) & categories):
            continue
        if query and not all(word in f"{raw['owner']} {raw['name']} {info.get('description', '')}".casefold()
                             for word in query.casefold().split()):
            continue
        rows.append(ThunderstoreListing(
            namespace=raw["owner"], name=raw["name"], description=info.get("description", ""),
            icon_url=info.get("icon", ""), download_count=sum(v.get("downloads", 0) for v in raw["versions"]),
            rating_count=raw.get("rating_score", 0), size=info.get("file_size", 0),
            last_updated=raw.get("date_updated", ""), datetime_created=raw.get("date_created", ""),
            categories=[(c, c, c) for c in sorted(categories)], is_deprecated=raw.get("is_deprecated", False),
            is_nsfw=raw.get("has_nsfw_content", False), is_pinned=raw.get("is_pinned", False), community=community))
    field = {"newest": "datetime_created", "most-downloaded": "download_count", "top-rated": "rating_count"}.get(ordering, "last_updated")
    rows.sort(key=lambda row: (getattr(row, field), row.package_id), reverse=True)
    return rows[page * PAGE_SIZE:(page + 1) * PAGE_SIZE], len(rows)


def resolve(link, progress_cb=None):
    from Thunderstore.thunderstore_requirements import ResolvedPackage, ResolveResult
    packages = catalogue(link.community, force=True)
    fallback = None
    fallback_community = (link.community if link.thunderstore_community is None
                          else link.thunderstore_community)
    requirements = {link.package_id: link.version}
    chosen = {}
    allow_pre = include_prereleases()
    for _ in range(1024):
        pending = [(link.package_id, link.version, 0)]
        visited, built, failed = set(), {}, []
        while pending:
            identity, minimum, depth = pending.pop()
            if identity in visited:
                continue
            visited.add(identity)
            minimum = max((minimum, requirements.get(identity, minimum)), key=version_key)
            source = "hexium"
            candidates = packages.get(identity, {}).get("versions", [])
            def eligible(v):
                number = v["version_number"]
                return (v.get("is_active", True) and (number == link.version if depth == 0 else
                        version_key(number) >= version_key(minimum) and
                        (allow_pre or "-" not in number or "-" in minimum)))
            info = next((v for v in candidates if eligible(v)), None)
            if info is None and depth and fallback_community:
                if fallback is None:
                    fallback = catalogue(fallback_community, force=True, source="thunderstore")
                info = next((v for v in fallback.get(identity, {}).get("versions", []) if eligible(v)), None)
                source = "thunderstore"
            if info is None:
                failed.append(f"{identity}-{minimum}")
                continue
            namespace, name, _ = split_full_name(info["full_name"])
            pkg = ResolvedPackage(namespace=namespace, name=name, version=info["version_number"],
                                  download_url=info.get("download_url", ""), file_size=info.get("file_size", 0),
                                  description=info.get("description", ""), website_url=info.get("website_url", ""),
                                  icon_url=info.get("icon", ""), dependencies=list(info.get("dependencies") or []),
                                  depth=depth, source=source)
            built[identity] = pkg
            if progress_cb:
                progress_cb(f"{source}: {pkg.full_name}")
            for dep in pkg.dependencies:
                ns, name, version = split_full_name(dep)
                if not ns:
                    failed.append(str(dep))
                    continue
                key = f"{ns}-{name}"
                requirements[key] = max((requirements.get(key, version), version), key=version_key)
                pending.append((key, requirements[key], depth + 1))
        signature = {key: (p.source, p.version) for key, p in built.items()}
        if signature == chosen:
            result = ResolveResult(failed=failed)
            ordered, active, done = [], set(), set()
            def visit(key):
                if key in active:
                    raise ValueError("Dependency cycle")
                if key in done or key not in built:
                    return
                active.add(key)
                for dep in built[key].dependencies:
                    ns, name, minimum = split_full_name(dep)
                    target = built.get(f"{ns}-{name}")
                    if target is None or version_key(target.version) < version_key(minimum):
                        raise ValueError(f"Unsatisfied dependency: {dep}")
                    visit(f"{ns}-{name}")
                active.remove(key)
                done.add(key)
                ordered.append(built[key])
            if not failed:
                visit(link.package_id)
            result.packages = ordered
            return result
        chosen = signature
    raise ValueError("Dependency graph exceeds the resolution limit")
