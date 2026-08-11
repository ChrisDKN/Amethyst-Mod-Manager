"""
thunderstore_api.py
Browse/listing queries against Thunderstore's cyberstorm API.

Toolkit-neutral (no Qt): the browser view is pure UI + threading on top of this,
mirroring how ``Nexus/nexus_api.py`` relates to ``gui_qt/nexus_browser_view.py``.

Every call here is unauthenticated - Thunderstore needs no API key, no OAuth and
imposes no rate limit on reads. All requests go through
:func:`Thunderstore.thunderstore_download.fetch_json`, which carries the explicit
User-Agent Cloudflare demands (the default ``Python-urllib/3.x`` is 403ed).

Endpoint quirks, all verified live - read before editing:

* The search parameter is ``q``. ``search=`` is **silently ignored** and returns
  the unfiltered page with HTTP 200, which looks like working code.
* ``ordering`` takes slugs (``last-updated``, ``newest``, ``most-downloaded``,
  ``top-rated``). Django-style ``-download_count`` values return HTTP 400.
* ``included_categories`` / ``excluded_categories`` take category **ids**.
  A ``categories=`` parameter exists in neither form and is ignored.
* ``section`` takes a section **uuid**, not its slug.
* ``deprecated=True`` / ``nsfw=True`` *widen* the result set; both default False.
* Pages are **1-based** here. The views keep a 0-based page index (house
  convention), so the +1 happens in :func:`listing_url` and nowhere else.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from urllib.parse import quote, urlencode

from Thunderstore.thunderstore_download import fetch_json

BASE = "https://thunderstore.io"

# Label → API ordering slug, in the order the Sort dropdown shows them.
ORDERINGS: list[tuple[str, str]] = [
    ("Last updated", "last-updated"),
    ("Newest", "newest"),
    ("Most downloaded", "most-downloaded"),
    ("Top rated", "top-rated"),
]

# The listing endpoint's page size is fixed server-side - it ignores any
# page-size parameter, so there is no "per page" choice to offer.
PAGE_SIZE = 20


@dataclass
class ThunderstoreListing:
    """One package as it appears in a community's browse listing."""

    namespace: str = ""
    name: str = ""
    description: str = ""
    icon_url: str = ""
    download_count: int = 0
    rating_count: int = 0
    size: int = 0                 # bytes (NOT KB - see the browser's adapter)
    last_updated: str = ""        # ISO-8601
    datetime_created: str = ""    # ISO-8601
    categories: list = field(default_factory=list)   # [(id, name, slug)]
    is_deprecated: bool = False
    is_nsfw: bool = False
    is_pinned: bool = False
    community: str = ""

    @property
    def package_id(self) -> str:
        """Version-independent identity, ``{namespace}-{name}``."""
        return f"{self.namespace}-{self.name}"


@dataclass
class ThunderstoreFilters:
    """A community's available category / section filters."""

    categories: list = field(default_factory=list)   # [(id, name, slug)]
    sections: list = field(default_factory=list)     # [(uuid, name, slug, priority)]


def package_url(community: str, namespace: str, name: str) -> str:
    """The package's public page.

    Only the community-scoped form resolves. The bare
    ``/package/{ns}/{name}/`` URL - which the API itself reports as
    ``package_url`` - returns 404 in a browser.
    """
    return f"{BASE}/c/{community}/p/{namespace}/{name}/"


def community_url(community: str) -> str:
    """The community's browse page."""
    return f"{BASE}/c/{community}/"


def listing_url(community: str, *, page: int = 0, query: str = "",
                ordering: str = "", included_categories=None,
                excluded_categories=None, section: str = "",
                deprecated: bool = False, nsfw: bool = False) -> str:
    """Build a cyberstorm listing URL. *page* is 0-based (the API is 1-based)."""
    params: list[tuple[str, str]] = [("page", str(max(0, int(page)) + 1))]
    # NB: 'q', never 'search' - see the module docstring.
    if query:
        params.append(("q", query))
    if ordering:
        params.append(("ordering", ordering))
    for cid in (included_categories or []):
        params.append(("included_categories", str(cid)))
    for cid in (excluded_categories or []):
        params.append(("excluded_categories", str(cid)))
    if section:
        params.append(("section", section))
    # Only send these when enabled; they widen the result set and False is the
    # server-side default anyway.
    if deprecated:
        params.append(("deprecated", "True"))
    if nsfw:
        params.append(("nsfw", "True"))
    return (f"{BASE}/api/cyberstorm/listing/{quote(community)}/"
            f"?{urlencode(params)}")


def _categories_of(raw) -> list:
    out: list = []
    for c in raw or []:
        if not isinstance(c, dict):
            continue
        out.append((str(c.get("id") or ""), c.get("name") or "",
                    c.get("slug") or ""))
    return out


def parse_listing(raw: dict, community: str = "") -> ThunderstoreListing:
    """Build a :class:`ThunderstoreListing` from one listing result dict."""
    def _int(key):
        try:
            return int(raw.get(key) or 0)
        except (TypeError, ValueError):
            return 0

    return ThunderstoreListing(
        namespace=raw.get("namespace") or "",
        name=raw.get("name") or "",
        description=raw.get("description") or "",
        icon_url=raw.get("icon_url") or "",
        download_count=_int("download_count"),
        rating_count=_int("rating_count"),
        size=_int("size"),
        last_updated=raw.get("last_updated") or "",
        datetime_created=raw.get("datetime_created") or "",
        categories=_categories_of(raw.get("categories")),
        is_deprecated=bool(raw.get("is_deprecated")),
        is_nsfw=bool(raw.get("is_nsfw")),
        is_pinned=bool(raw.get("is_pinned")),
        community=raw.get("community_identifier") or community,
    )


def fetch_listing(community: str, **kw) -> tuple[list, int]:
    """
    Fetch one page of a community's listing.

    Returns ``(packages, total_count)``; ``([], 0)`` on any failure. The exact
    total lets the caller show a real "page N / M" instead of guessing from
    whether the page came back full.
    """
    if not community:
        return [], 0
    data = fetch_json(listing_url(community, **kw))
    if not isinstance(data, dict):
        return [], 0
    results = data.get("results")
    if not isinstance(results, list):
        return [], 0
    try:
        count = int(data.get("count") or 0)
    except (TypeError, ValueError):
        count = 0
    return [parse_listing(r, community) for r in results
            if isinstance(r, dict)], count


def total_pages(count: int) -> int:
    """Page count for *count* results at the server's fixed page size."""
    if count <= 0:
        return 1
    return (count + PAGE_SIZE - 1) // PAGE_SIZE


def fetch_filters(community: str) -> ThunderstoreFilters | None:
    """A community's category + section filters, or None on failure."""
    if not community:
        return None
    data = fetch_json(f"{BASE}/api/cyberstorm/community/{quote(community)}"
                      f"/filters/")
    if not isinstance(data, dict):
        return None
    sections: list = []
    for s in data.get("sections") or []:
        if not isinstance(s, dict):
            continue
        try:
            prio = int(s.get("priority") or 0)
        except (TypeError, ValueError):
            prio = 0
        sections.append((s.get("uuid") or "", s.get("name") or "",
                         s.get("slug") or "", prio))
    # Highest priority first - the site orders its section tabs this way.
    sections.sort(key=lambda x: -x[3])
    return ThunderstoreFilters(
        categories=_categories_of(data.get("package_categories")),
        sections=sections,
    )


def fetch_versions(namespace: str, name: str) -> list:
    """Every published version of a package, newest-first order NOT guaranteed.

    The single place that knows this endpoint - the version picker and anything
    else needing a version list both come through here.
    """
    if not (namespace and name):
        return []
    data = fetch_json(f"{BASE}/api/cyberstorm/package/"
                      f"{quote(namespace)}/{quote(name)}/versions/")
    return data if isinstance(data, list) else []


def fetch_latest_version(namespace: str, name: str) -> str:
    """The newest published version string, or "".

    Uses the experimental package endpoint rather than the full version list -
    it returns ``latest.version_number`` directly, a much smaller payload than
    every version of a package with hundreds of releases.
    """
    if not (namespace and name):
        return ""
    data = fetch_json(f"{BASE}/api/experimental/package/"
                      f"{quote(namespace)}/{quote(name)}/")
    if not isinstance(data, dict):
        return ""
    latest = data.get("latest")
    if not isinstance(latest, dict):
        return ""
    return latest.get("version_number") or ""
