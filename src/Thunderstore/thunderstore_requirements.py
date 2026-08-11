"""
thunderstore_requirements.py
Resolve a Thunderstore package's transitive dependency graph.

Thunderstore dependencies are exact pinned ``{namespace}-{name}-{version}``
triples on every package version, so the graph can be walked precisely -
no guessing from free-text requirement lists the way the Nexus side has to.

Version conflicts are real and routine
--------------------------------------
Walking one ordinary mod (GamerIsaac-Base_Deconstruct_Fix) yields::

    Base_Deconstruct_Fix -> QModManager-4.4.3
                         -> SMLHelper-2.13.4 -> QModManager-4.4.0

QModManager is pinned at BOTH 4.4.0 and 4.4.3. A package is one folder in
staging, so exactly one version can win. This resolver takes the HIGHEST
pinned version, which is what r2modman/Gale effectively do: in practice
Thunderstore pins express "at least this", and mod authors expect the newest
loader to satisfy older pins.

The graph also revisits shared roots constantly (BepInExPack appears four
times in that same walk), so every package version is fetched at most once.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from Nexus.nexus_update_checker import _parse_version
from Thunderstore.thunderstore_download import fetch_json
from Thunderstore.thunderstore_meta import parse_full_name
from Utils.app_log import app_log

# Guard against a pathological/cyclic graph pulling the whole catalogue.
_MAX_PACKAGES = 200


@dataclass
class ResolvedPackage:
    """One package version in a resolved dependency graph."""

    namespace: str = ""
    name: str = ""
    version: str = ""
    download_url: str = ""
    file_size: int = 0
    description: str = ""
    website_url: str = ""
    icon_url: str = ""
    dependencies: list = field(default_factory=list)
    # Depth 0 = the package the user asked for; >0 = pulled in as a dependency.
    depth: int = 0
    # Every version this package was pinned at across the graph, when the
    # resolver had to choose between conflicting pins.
    pinned_versions: list = field(default_factory=list)

    @property
    def package_id(self) -> str:
        return f"{self.namespace}-{self.name}"

    @property
    def full_name(self) -> str:
        return f"{self.namespace}-{self.name}-{self.version}"

    @property
    def is_root(self) -> bool:
        return self.depth == 0


@dataclass
class ResolveResult:
    """Outcome of a dependency walk."""

    packages: list = field(default_factory=list)      # ResolvedPackage, root first
    conflicts: list = field(default_factory=list)     # (package_id, [versions], chosen)
    failed: list = field(default_factory=list)        # full_name strings that 404'd
    truncated: bool = False                           # hit _MAX_PACKAGES

    @property
    def dependencies(self) -> list:
        """Everything except the root package."""
        return [p for p in self.packages if not p.is_root]


def version_api_url(namespace: str, name: str, version: str,
                    host: str = "thunderstore.io") -> str:
    """Experimental per-version endpoint - the only one exposing exact deps.

    Cyberstorm's version detail returns a ``dependency_count`` but not the
    dependency list, so it cannot drive a traversal.
    """
    return (f"https://{host}/api/experimental/package/"
            f"{namespace}/{name}/{version}/")


def size_api_url(namespace: str, name: str, version: str,
                 host: str = "thunderstore.io") -> str:
    """Cyberstorm per-version endpoint - the only one exposing the zip size."""
    return (f"https://{host}/api/cyberstorm/package/"
            f"{namespace}/{name}/v/{version}/")


def _fetch_version(namespace: str, name: str, version: str,
                   host: str = "thunderstore.io") -> dict | None:
    """Fetch one package version's metadata, merging both APIs.

    Neither endpoint is sufficient alone: the experimental one carries
    ``dependencies`` but no size, while cyberstorm's carries ``size`` but only
    a ``dependency_count``. Size is the ONLY integrity check Thunderstore
    offers (no hashes exist), so it is worth the second request.
    """
    data = fetch_json(version_api_url(namespace, name, version, host))
    if not isinstance(data, dict):
        return None
    if not data.get("file_size"):
        extra = fetch_json(size_api_url(namespace, name, version, host))
        if isinstance(extra, dict) and extra.get("size"):
            data["file_size"] = extra["size"]
    return data


def _newest(versions) -> str:
    """Pick the highest version string, tolerating unparseable ones."""
    best = ""
    best_key = None
    for v in versions:
        key = _parse_version(v)
        if key is None:
            continue
        if best_key is None or key > best_key:
            best, best_key = v, key
    # Every version unparseable → fall back to the first seen, so the caller
    # still gets a usable pin rather than an empty string.
    if not best:
        for v in versions:
            if v:
                return v
    return best


def resolve_dependencies(link, *, host: str = "thunderstore.io",
                         progress_cb: Optional[Callable[[str], None]] = None,
                         ) -> ResolveResult:
    """
    Walk the full transitive dependency graph for a parsed ror2mm link.

    Returns a :class:`ResolveResult` whose ``packages`` are ordered
    dependencies-FIRST (so installing in order satisfies each package's
    requirements before it lands), with the root package last.
    """
    result = ResolveResult()

    # Breadth-first walk, collecting every pin seen per package id.
    pins: dict[str, set] = {}
    depth_of: dict[str, int] = {}
    queue = [(link.namespace, link.name, link.version, 0)]
    fetched: dict[str, dict] = {}      # full_name → version payload
    seen_edges: set = set()

    while queue:
        ns, name, ver, depth = queue.pop(0)
        pkg_id = f"{ns}-{name}"
        pins.setdefault(pkg_id, set()).add(ver)
        # Keep the SHALLOWEST depth: a package reachable both directly and
        # transitively is the more important of the two.
        if pkg_id not in depth_of or depth < depth_of[pkg_id]:
            depth_of[pkg_id] = depth

        full = f"{ns}-{name}-{ver}"
        if full in seen_edges:
            continue
        seen_edges.add(full)

        if len(fetched) >= _MAX_PACKAGES:
            result.truncated = True
            app_log(f"thunderstore: dependency walk hit the {_MAX_PACKAGES} "
                    "package cap - truncating")
            break

        if progress_cb:
            progress_cb(full)
        data = _fetch_version(ns, name, ver, host)
        if not isinstance(data, dict):
            result.failed.append(full)
            continue
        fetched[full] = data

        for dep in data.get("dependencies") or []:
            dns, dname, dver = parse_full_name(str(dep))
            if not (dns and dname and dver):
                app_log(f"thunderstore: skipping unparseable dependency {dep!r}")
                continue
            queue.append((dns, dname, dver, depth + 1))

    # Choose one version per package and record the conflicts.
    chosen: dict[str, str] = {}
    for pkg_id, versions in pins.items():
        if len(versions) > 1:
            pick = _newest(versions)
            chosen[pkg_id] = pick
            result.conflicts.append((pkg_id, sorted(versions), pick))
            app_log(f"thunderstore: {pkg_id} pinned at "
                    f"{sorted(versions)} - taking {pick}")
        else:
            chosen[pkg_id] = next(iter(versions))

    # A conflict winner may be a version we never fetched (it was queued but
    # deduped behind a different pin) - fetch the chosen one now.
    for pkg_id, ver in chosen.items():
        full = f"{pkg_id}-{ver}"
        if full in fetched:
            continue
        ns, name, _v = parse_full_name(full)
        if not ns:
            continue
        data = _fetch_version(ns, name, ver, host)
        if isinstance(data, dict):
            fetched[full] = data
        elif full not in result.failed:
            result.failed.append(full)

    built: list = []
    for pkg_id, ver in chosen.items():
        full = f"{pkg_id}-{ver}"
        data = fetched.get(full)
        if data is None:
            continue
        ns, name, _v = parse_full_name(full)
        try:
            size = int(data.get("file_size") or 0)
        except (TypeError, ValueError):
            size = 0
        built.append(ResolvedPackage(
            namespace=ns, name=name, version=ver,
            download_url=data.get("download_url") or "",
            file_size=size,
            description=data.get("description") or "",
            website_url=data.get("website_url") or "",
            icon_url=data.get("icon") or "",
            dependencies=list(data.get("dependencies") or []),
            depth=depth_of.get(pkg_id, 0),
            pinned_versions=sorted(pins.get(pkg_id, {ver})),
        ))

    # Deepest first, so a package's own dependencies are installed before it.
    built.sort(key=lambda p: (-p.depth, p.package_id))
    result.packages = built
    return result


def filter_already_installed(packages, staging_root: Path,
                             keep_root: bool = True) -> tuple[list, list]:
    """
    Split *packages* into (to_install, already_present).

    A package counts as present when a staging folder carries a
    ``[thunderstore]`` section with the same package id AND a version at least
    as new as the one required - matching the highest-wins rule used above, so
    an existing newer loader is not downgraded.

    Folders installed from an archive (no Thunderstore metadata) are also
    matched by folder name, so a manually-installed dependency is not
    duplicated.

    *keep_root* (default) exempts the package the user actually asked for
    (``depth == 0``) from all of this: an explicit install must always run.
    Without it, re-installing the current version would do nothing at all, and
    picking an OLDER version in the Change Version tab would silently drop the
    requested mod and install only its dependencies - the newer installed copy
    reads as "already satisfied".
    """
    from Thunderstore.thunderstore_meta import read_meta

    staging_root = Path(staging_root)
    installed: dict[str, str] = {}     # package_id → version
    folder_names: set = set()
    if staging_root.is_dir():
        for folder in staging_root.iterdir():
            if not folder.is_dir():
                continue
            folder_names.add(folder.name.lower())
            meta_path = folder / "meta.ini"
            if not meta_path.is_file():
                continue
            try:
                meta = read_meta(meta_path)
            except Exception:
                continue
            if meta.package_id:
                prev = installed.get(meta.package_id)
                if prev is None or (_parse_version(meta.version) or ()) > (
                        _parse_version(prev) or ()):
                    installed[meta.package_id] = meta.version

    to_install: list = []
    present: list = []
    for pkg in packages:
        # The explicitly requested package is never "already installed" - the
        # user asked for THIS version (reinstall, upgrade or downgrade).
        if keep_root and getattr(pkg, "is_root", False):
            to_install.append(pkg)
            continue
        have = installed.get(pkg.package_id)
        if have:
            hv, wv = _parse_version(have), _parse_version(pkg.version)
            if hv is not None and wv is not None and hv >= wv:
                present.append(pkg)
                continue
            # Installed but older → reinstall at the required version.
            to_install.append(pkg)
            continue
        if pkg.full_name.lower() in folder_names:
            present.append(pkg)
            continue
        to_install.append(pkg)
    return to_install, present
