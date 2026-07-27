"""
wiki_sync.py
Fetch the Amethyst-Mod-Manager GitHub wiki (page list, page markdown, images)
so the manager can display it in-app, always reflecting the live wiki.

GitHub wikis are separate git repositories and are NOT exposed through the
REST contents API, so this module uses the two public surfaces that do work
without auth:

* ``/wiki/_pages`` — an HTML index of every page. Scraped for (slug, title)
  pairs in the order GitHub lists them. This is plain github.com HTML, not the
  REST API, so it does not consume the 60 requests/hour unauthenticated quota.
* ``raw.githubusercontent.com/wiki/<owner>/<repo>/<slug>.md`` — the raw
  markdown source of a page.

Everything goes through :mod:`Utils.gh_cache`, which adds ETag conditional
requests (a 304 is free) plus a per-URL throttle, and keeps the last body on
disk — so repeat visits are cheap and the wiki stays readable offline once a
page has been viewed.

All functions return ``None``/empty rather than raising: a wiki that cannot be
reached must never break the UI.
"""

from __future__ import annotations

import html
import re
import urllib.parse
from typing import List, Optional, Tuple

from Utils.gh_cache import fetch, fetch_text

#: owner/repo whose wiki is displayed.
WIKI_REPO = "ChrisDKN/Amethyst-Mod-Manager"

_PAGES_URL = f"https://github.com/{WIKI_REPO}/wiki/_pages"
_RAW_BASE = f"https://raw.githubusercontent.com/wiki/{WIKI_REPO}/"
_WEB_BASE = f"https://github.com/{WIKI_REPO}/wiki/"

# Throttles. The page list and page bodies can change any time the wiki is
# edited, so they re-check often; images are content-addressed attachment URLs
# whose bytes never change, so they are effectively fetched once.
_LIST_INTERVAL = 300.0
_PAGE_INTERVAL = 300.0
_IMAGE_INTERVAL = 30 * 24 * 3600.0

# <a href="/owner/repo/wiki/<slug>">Title</a> — the shape of every entry in
# the _pages index. Slugs are already percent-encoded in the href.
_ANCHOR_RE = re.compile(
    r'<a[^>]+href="/' + re.escape(WIKI_REPO) + r'/wiki/([^"/#?]+)"[^>]*>([^<]*)</a>'
)

# Action links that share the /wiki/ prefix but are not pages.
_RESERVED_SLUGS = frozenset({
    "_new", "_edit", "_history", "_compare", "_access", "_pages",
})

# Hosts an <img> in a wiki page may be fetched from. Wiki content is authored
# on GitHub, but it is still remote text driving network requests — keep it
# from pointing the manager at arbitrary third-party hosts.
_IMAGE_HOSTS = frozenset({
    "github.com",
    "www.github.com",
    "raw.githubusercontent.com",
    "user-images.githubusercontent.com",
    "camo.githubusercontent.com",
    "objects.githubusercontent.com",
    "avatars.githubusercontent.com",
})

# The wiki has no _Sidebar.md and GitHub omits Home from _pages, so it is
# synthesised as the first entry.
_HOME_SLUG = "Home"

# [[Target]] / [[Label|Target]] — MediaWiki-style links GitHub renders but
# markdown does not.
_WIKILINK_RE = re.compile(r"\[\[([^\]|]+?)(?:\|([^\]]+?))?\]\]")

# <img ...> tags and the two attributes worth keeping off them.
_IMG_TAG_RE = re.compile(r"<img\b[^>]*?/?>", re.IGNORECASE)
_IMG_SRC_RE = re.compile(r"\bsrc\s*=\s*(?:\"([^\"]*)\"|'([^']*)'|([^\s>]+))",
                         re.IGNORECASE)
_IMG_ALT_RE = re.compile(r"\balt\s*=\s*(?:\"([^\"]*)\"|'([^']*)'|([^\s>]+))",
                         re.IGNORECASE)


def page_web_url(slug: str) -> str:
    """Return the github.com URL for a wiki page slug."""
    return _WEB_BASE + slug


def _safe_slug(slug: str) -> "str | None":
    """Validate a page slug as a single path component, or None."""
    if not slug or slug in _RESERVED_SLUGS:
        return None
    decoded = urllib.parse.unquote(slug)
    if "/" in decoded or "\\" in decoded or ".." in decoded:
        return None
    return slug


def list_pages(*, force: bool = False) -> List[Tuple[str, str]]:
    """Return the wiki's [(slug, title), ...] in GitHub's own listing order.

    Home is always first. Returns [] if the index could not be fetched and
    nothing was cached.
    """
    body = fetch_text(
        _PAGES_URL,
        accept="text/html",
        min_interval=0.0 if force else _LIST_INTERVAL,
        force=force,
    )
    if not body:
        return []

    pages: List[Tuple[str, str]] = []
    seen = set()
    for slug, title in _ANCHOR_RE.findall(body):
        slug = _safe_slug(slug)
        if slug is None or slug in seen:
            continue
        seen.add(slug)
        label = html.unescape(title).strip()
        if not label:
            label = urllib.parse.unquote(slug).replace("-", " ")
        pages.append((slug, label))

    if not pages:
        return []
    if _HOME_SLUG not in seen:
        pages.insert(0, (_HOME_SLUG, "Home"))
    return pages


def fetch_page(slug: str, *, force: bool = False) -> Optional[str]:
    """Return a wiki page's raw markdown, or None if it could not be fetched."""
    slug = _safe_slug(slug)
    if slug is None:
        return None
    return fetch_text(
        _RAW_BASE + slug + ".md",
        accept="text/plain",
        min_interval=0.0 if force else _PAGE_INTERVAL,
        force=force,
    )


def fetch_image(url: str) -> Optional[bytes]:
    """Return the bytes of a wiki image, or None (rejects non-GitHub hosts)."""
    try:
        parsed = urllib.parse.urlparse(url)
    except Exception:
        return None
    if parsed.scheme != "https" or parsed.hostname not in _IMAGE_HOSTS:
        return None
    return fetch(url, accept="image/*", min_interval=_IMAGE_INTERVAL, timeout=30.0)


def resolve_link(href: str) -> Optional[str]:
    """Map a link found in a wiki page to a wiki slug, or None if external.

    Handles both absolute github.com wiki URLs and the relative hrefs Qt
    produces for bare page references.
    """
    if not href:
        return None
    try:
        parsed = urllib.parse.urlparse(href)
    except Exception:
        return None
    if parsed.scheme in ("http", "https"):
        if parsed.hostname not in ("github.com", "www.github.com"):
            return None
        prefix = f"/{WIKI_REPO}/wiki/"
        if not parsed.path.startswith(prefix):
            return None
        candidate = parsed.path[len(prefix):]
    elif parsed.scheme:
        # mailto:, file:, anything else — not a wiki page.
        return None
    else:
        candidate = parsed.path
    candidate = candidate.strip("/")
    if not candidate:
        return _HOME_SLUG
    return _safe_slug(candidate)


def _attr(regex, tag: str) -> str:
    """Return an attribute's value from an HTML tag, or ''."""
    m = regex.search(tag)
    if not m:
        return ""
    return next((g for g in m.groups() if g is not None), "")


def to_display_markdown(text: str) -> str:
    """Adapt raw wiki markdown for rendering in a QTextBrowser.

    Two adjustments, neither of which touches the wiki itself:

    * ``[[Page]]`` / ``[[Label|Page]]`` wiki links become ordinary markdown
      links so they are clickable instead of showing as literal brackets.
    * ``<img>`` tags — how GitHub records every pasted screenshot — are
      rewritten as native ``![alt](src)`` markdown images.

    The image rewrite matters for layout, not just tidiness. Left as HTML, the
    tag reaches Qt's markdown importer as a raw *HTML block*, which it splices
    in as a fragment instead of giving it its own paragraph: the picture ends
    up welded to a neighbouring block of text and drifts sideways across the
    page. As markdown, each image becomes a normal paragraph and sits on its
    own line. Dropping the tag's ``width``/``height`` also lets the viewer
    scale wide screenshots down instead of forcing horizontal scrolling.
    """
    def _wikilink(m):
        first, second = m.group(1).strip(), m.group(2)
        # GitHub's form is [[Label|Target]]; with one part it is both.
        label, target = (first, first) if second is None else (first, second.strip())
        return "[{0}]({1})".format(label, urllib.parse.quote(target.replace(" ", "-")))

    def _img(m):
        tag = m.group(0)
        src = _attr(_IMG_SRC_RE, tag).strip()
        if not src:
            return ""
        alt = _attr(_IMG_ALT_RE, tag).replace("[", "").replace("]", "")
        # Angle-bracket form keeps a destination containing spaces or brackets
        # from terminating the markdown link early.
        if any(ch in src for ch in " ()<>"):
            src = "<{0}>".format(src.replace("<", "").replace(">", ""))
        return "![{0}]({1})".format(alt, src)

    text = _WIKILINK_RE.sub(_wikilink, text)
    return _IMG_TAG_RE.sub(_img, text)
