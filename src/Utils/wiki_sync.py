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
  markdown source of a page, ``_Sidebar.md`` included.

The navigation shown in the app is the wiki's own ``_Sidebar.md`` when it has
one — same grouping, order and wording as github.com — with ``_pages`` used to
append anything the sidebar does not link to, and as the whole list if there is
no sidebar.

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
_SIDEBAR_INTERVAL = 300.0
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

# GitHub omits Home from _pages, so it is synthesised as the first entry.
_HOME_SLUG = "Home"

#: The wiki's own navigation page, if it has one.
_SIDEBAR_SLUG = "_Sidebar"

#: Row kinds returned by :func:`nav_rows`. Headers carry no slug;
#: ``SIDEBAR_OTHER`` heads the pages the sidebar itself does not link to, and
#: its label is left to the caller so it can be translated.
SIDEBAR_PAGE = "page"
SIDEBAR_HEADER = "header"
SIDEBAR_OTHER = "other"

#: Sidebar syntax: a bullet/numbered item, a heading, an inline link, and the
#: emphasis runs a label may be wrapped in.
_SIDEBAR_ITEM_RE = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+(.*)$")
_SIDEBAR_HEADING_RE = re.compile(r"^#{1,6}\s+(.*)$")
_MD_LINK_RE = re.compile(r"\[([^\]]*)\]\(\s*<?([^)\s>]*)>?(?:\s+\"[^\"]*\")?\s*\)")
_EMPHASIS_EDGE_RE = re.compile(r"^(?:\*{1,3}|_{1,3}|`)+|(?:\*{1,3}|_{1,3}|`)+$")
#: A horizontal rule / decorative divider — nothing to show in a list.
_RULE_LINE_RE = re.compile(r"^[-*_ ]+$")

# [[Target]] / [[Label|Target]] — MediaWiki-style links GitHub renders but
# markdown does not.
_WIKILINK_RE = re.compile(r"\[\[([^\]|]+?)(?:\|([^\]]+?))?\]\]")

# <img ...> tags and the two attributes worth keeping off them.
_IMG_TAG_RE = re.compile(r"<img\b[^>]*?/?>", re.IGNORECASE)
#: An <img> that is the whole line — how GitHub records a pasted screenshot.
_LONE_IMG_LINE_RE = re.compile(r"^\s*<img\b[^>]*?/?>\s*$", re.IGNORECASE)
_IMG_SRC_RE = re.compile(r"\bsrc\s*=\s*(?:\"([^\"]*)\"|'([^']*)'|([^\s>]+))",
                         re.IGNORECASE)
_IMG_ALT_RE = re.compile(r"\balt\s*=\s*(?:\"([^\"]*)\"|'([^']*)'|([^\s>]+))",
                         re.IGNORECASE)

# HTML comments, and fenced code blocks (protected from every rewrite below).
_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
_FENCE_RE = re.compile(r"^(\s{0,3})(`{3,}|~{3,})", re.MULTILINE)
_BLANKS_RE = re.compile(r"\n{3,}")

# The HTML this wiki actually uses in its pages, and the markdown each maps to.
_A_RE = re.compile(r"<a\b[^>]*?\bhref\s*=\s*(?:\"([^\"]*)\"|'([^']*)'|([^\s>]+))"
                   r"[^>]*>(.*?)</a\s*>", re.IGNORECASE | re.DOTALL)
_HEADING_RE = re.compile(r"<h([1-6])\b[^>]*>(.*?)</h\1\s*>",
                         re.IGNORECASE | re.DOTALL)
_BR_RE = re.compile(r"<br\b[^>]*/?>", re.IGNORECASE)
_HR_RE = re.compile(r"<hr\b[^>]*/?>", re.IGNORECASE)
_EMPHASIS_RES = (
    (re.compile(r"</?(?:b|strong)\s*>", re.IGNORECASE), "**"),
    (re.compile(r"</?(?:i|em)\s*>", re.IGNORECASE), "*"),
    (re.compile(r"</?code\s*>", re.IGNORECASE), "`"),
)
#: Wrappers that only exist to group/position content — each becomes a break.
_BLOCK_TAG_RE = re.compile(
    r"</?(?:p|div|center|span|section|details|summary|blockquote)\b[^>]*>",
    re.IGNORECASE)
_ANY_TAG_RE = re.compile(r"</?[a-zA-Z][^>]*>")
#: Real HTML element names, so a line opening with a placeholder like
#: ``<game>`` or ``<user>`` (both appear in this wiki's path examples) is
#: treated as the prose it is rather than as the start of an HTML block.
_HTML_BLOCK_START_RE = re.compile(
    r"^\s{0,3}</?(?:p|div|span|center|section|details|summary|blockquote"
    r"|table|thead|tbody|tr|td|th|ul|ol|li|dl|dt|dd|h[1-6]|img|a|br|hr|pre"
    r"|picture|video|figure|figcaption|b|i|em|strong|code|kbd|sub|sup)\b",
    re.IGNORECASE)
_CENTERED_RE = re.compile(r"align\s*=\s*[\"']?center", re.IGNORECASE)
#: A <table> block, left as HTML — Qt imports those into real tables. It ends
#: at </table> rather than at a blank line, since long tables often have both.
_TABLE_RE = re.compile(r"^\s{0,3}<table\b", re.IGNORECASE)
_TABLE_END_RE = re.compile(r"</table\s*>", re.IGNORECASE)
#: Leading markdown block marker, so the centring mark goes after it.
_MD_PREFIX_RE = re.compile(r"^(\s*(?:#{1,6}\s+|[-*+]\s+|\d+[.)]\s+|>\s*)?)(.*)$")

#: Zero-width marker: content the wiki centres, for the viewer to align.
#: Invisible either way, so a page that skips the post-import pass just reads
#: as ordinary left-aligned prose.
CENTER_MARK = "⁢"

#: URL scheme the profile share codes printed in wiki pages are rewritten to,
#: so the viewer can offer to import one on a click. The link target is an
#: index into the list :func:`to_display_markdown` fills in — a code is a
#: kilobyte of base64, far too long to carry in a link.
SHARE_CODE_SCHEME = "ammshare"

#: A share code as it appears in a page: the ``AMMCODE<n>:`` tag from
#: :mod:`Utils.profile_export` followed by its urlsafe-base64 payload. The
#: length floor keeps a bare mention of the format from matching, and the
#: optional backticks let an inline-code span be swallowed whole — a markdown
#: link inside one would render as its own literal text.
_SHARE_CODE_RE = re.compile(r"AMMCODE\d+:[A-Za-z0-9_-]{24,}={0,2}")
_SHARE_CODE_INLINE_RE = re.compile("`?(" + _SHARE_CODE_RE.pattern + ")`?")

#: How much of a code the link shows: enough to recognise it as the one the
#: page is talking about, short of a screenful of base64.
_CODE_LABEL_CHARS = 40


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


def fetch_sidebar(*, force: bool = False) -> Optional[str]:
    """Return the wiki's ``_Sidebar.md`` source, or None if it has none."""
    return fetch_text(
        _RAW_BASE + _SIDEBAR_SLUG + ".md",
        accept="text/plain",
        min_interval=0.0 if force else _SIDEBAR_INTERVAL,
        force=force,
    )


def _label(text: str) -> str:
    """Reduce one sidebar cell to plain text — tags, emphasis and entities out."""
    text = _ANY_TAG_RE.sub("", text).strip()
    text = _EMPHASIS_EDGE_RE.sub("", text).strip()
    return html.unescape(text)


def parse_sidebar(text: Optional[str]) -> List[Tuple[str, str, Optional[str]]]:
    """Parse ``_Sidebar.md`` into ``[(kind, label, slug), ...]`` rows.

    A bullet or heading holding a wiki link becomes a page row keeping the
    sidebar's own wording; any other non-blank line becomes a header row, which
    is how the sidebar's ``**Getting started**``-style group titles survive.
    Lines whose only link points off the wiki — the GitHub/Nexus footer — are
    dropped, since this list navigates pages.
    """
    if not text:
        return []
    text = _COMMENT_RE.sub("", text)
    text = _WIKILINK_RE.sub(_wikilink, text)

    rows: List[Tuple[str, str, Optional[str]]] = []
    for raw in text.split("\n"):
        line = raw.strip()
        if not line or _RULE_LINE_RE.match(line):
            continue
        item = _SIDEBAR_ITEM_RE.match(line)
        if item:
            line = item.group(1).strip()
        heading = _SIDEBAR_HEADING_RE.match(line)
        if heading:
            line = heading.group(1).strip()
        link = _MD_LINK_RE.search(line)
        if link:
            slug = resolve_link(html.unescape(link.group(2)))
            if slug:
                rows.append((SIDEBAR_PAGE, _label(link.group(1)) or slug, slug))
            continue
        label = _label(line)
        if label:
            rows.append((SIDEBAR_HEADER, label, None))
    return rows


def nav_rows(*, force: bool = False) -> List[Tuple[str, str, Optional[str]]]:
    """Return the navigation rows to show in the app's page list.

    The wiki's own sidebar when it has one, otherwise the flat ``_pages``
    index. Either way every page ends up reachable: pages the sidebar does not
    link to are appended under a :data:`SIDEBAR_OTHER` header, so a page added
    on GitHub — or one the sidebar links to under a mistyped slug — never goes
    missing from the app.
    """
    rows = parse_sidebar(fetch_sidebar(force=force))
    pages = list_pages(force=force)
    if not rows:
        return [(SIDEBAR_PAGE, title, slug) for slug, title in pages]

    listed = {slug for kind, _label_, slug in rows if kind == SIDEBAR_PAGE}
    if _HOME_SLUG not in listed:
        rows.insert(0, (SIDEBAR_PAGE, "Home", _HOME_SLUG))
        listed.add(_HOME_SLUG)
    extra = [(SIDEBAR_PAGE, title, slug)
             for slug, title in pages if slug not in listed]
    if extra:
        rows.append((SIDEBAR_OTHER, "", None))
        rows.extend(extra)
    return rows


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


def _wikilink(m) -> str:
    first, second = m.group(1).strip(), m.group(2)
    # GitHub's form is [[Label|Target]]; with one part it is both.
    label, target = (first, first) if second is None else (first, second.strip())
    return "[{0}]({1})".format(label, urllib.parse.quote(target.replace(" ", "-")))


def _img_markdown(tag: str) -> str:
    """Rewrite one ``<img>`` tag as a native ``![alt](src)`` image."""
    src = _attr(_IMG_SRC_RE, tag).strip()
    if not src:
        return ""
    alt = _attr(_IMG_ALT_RE, tag).replace("[", "").replace("]", "")
    # Angle-bracket form keeps a destination containing spaces or brackets
    # from terminating the markdown link early.
    if any(ch in src for ch in " ()<>"):
        src = "<{0}>".format(src.replace("<", "").replace(">", ""))
    return "![{0}]({1})".format(alt, src)


def _code_link(code: str, codes: List[str]) -> str:
    """Register *code* and return the markdown link standing in for it.

    The label is the code itself, truncated — no words, since this module is
    not translated and the viewer supplies the wording around it.
    """
    codes.append(code)
    label = (code if len(code) <= _CODE_LABEL_CHARS
             else code[:_CODE_LABEL_CHARS] + "…")
    return "[▶ {0}]({1}:{2})".format(label, SHARE_CODE_SCHEME, len(codes) - 1)


def _link_codes(text: str, codes: Optional[List[str]]) -> str:
    """Rewrite every share code in *text* as a clickable link, if asked to."""
    if codes is None or "AMMCODE" not in text:
        return text
    return _SHARE_CODE_INLINE_RE.sub(lambda m: _code_link(m.group(1), codes), text)


def _rewrite_code_fence(chunk: str, codes: Optional[List[str]]) -> str:
    """Turn a fenced block that is nothing but a share code into a link.

    Only that shape is touched: a link inside a fence renders as its own
    literal text, so the fence has to go — and a fence carrying anything else
    is a genuine code sample that must survive verbatim.
    """
    if codes is None or "AMMCODE" not in chunk:
        return chunk
    body = [line for line in chunk.split("\n") if not _FENCE_RE.match(line)]
    # Rejoined rather than matched line by line, so a code the author wrapped
    # over several lines is still recognised as the one code it is.
    joined = "".join("".join(line.split()) for line in body)
    if not _SHARE_CODE_RE.fullmatch(joined):
        return chunk
    return "\n\n{0}\n\n".format(_code_link(joined, codes))


def _split_fences(text: str) -> List[Tuple[bool, str]]:
    """Split *text* into (is_code, chunk) runs, ``is_code`` for fenced blocks.

    Everything the display rewrite does — dropping comments, converting wiki
    links, promoting images — must leave fenced code samples byte-identical.
    """
    chunks: List[Tuple[bool, str]] = []
    buf: List[str] = []
    fence: Optional[str] = None

    def flush(is_code: bool) -> None:
        if buf:
            chunks.append((is_code, "\n".join(buf) + "\n"))
            buf.clear()

    for line in text.split("\n"):
        m = _FENCE_RE.match(line)
        if fence is None:
            if m:
                flush(False)
                fence = m.group(2)[0]
            buf.append(line)
        else:
            buf.append(line)
            if m and m.group(2)[0] == fence:
                flush(True)
                fence = None
    flush(fence is not None)
    return chunks


def _html_to_markdown(region: str) -> str:
    """Convert one block of hand-written HTML into equivalent markdown.

    Qt's markdown importer takes an HTML block as an opaque fragment and
    splices it into whatever paragraph happens to be next, so a hand-written
    header comes out as one run-on line with the logo drawn across it. Turning
    the HTML into markdown first gives every piece its own block.
    """
    centered = bool(_CENTERED_RE.search(region))
    region = _IMG_TAG_RE.sub(lambda m: "\n\n" + _img_markdown(m.group(0)) + "\n\n",
                             region)
    region = _A_RE.sub(
        lambda m: "[{0}]({1})".format(
            _ANY_TAG_RE.sub("", m.group(4)).strip(),
            next(g for g in m.groups()[:3] if g is not None)),
        region)
    region = _HEADING_RE.sub(
        lambda m: "\n\n{0} {1}\n\n".format(
            "#" * int(m.group(1)), _ANY_TAG_RE.sub("", m.group(2)).strip()),
        region)
    region = _HR_RE.sub("\n\n---\n\n", region)
    region = _BR_RE.sub("  \n", region)
    for pattern, repl in _EMPHASIS_RES:
        region = pattern.sub(repl, region)
    region = _BLOCK_TAG_RE.sub("\n\n", region)
    region = _ANY_TAG_RE.sub("", region)
    region = html.unescape(region)
    # Indentation left behind by a stripped wrapper would otherwise survive as
    # a whitespace-only line, which markdown renders as an empty paragraph.
    region = re.sub(r"^[ \t]+$", "", region, flags=re.MULTILINE)
    region = _BLANKS_RE.sub("\n\n", region)
    if centered:
        # Only the line that opens each block is marked: a mark on a
        # continuation line would end up as a stray character mid-paragraph.
        lines = []
        at_block_start = True
        for line in region.split("\n"):
            if line.strip():
                if at_block_start:
                    line = _MD_PREFIX_RE.sub(r"\1" + CENTER_MARK + r"\2", line)
                at_block_start = False
            else:
                at_block_start = True
            lines.append(line)
        region = "\n".join(lines)
    return region


def _rewrite_prose(text: str, codes: Optional[List[str]] = None) -> str:
    """Apply the display rewrites to one non-code chunk of a wiki page."""
    text = _COMMENT_RE.sub("", text)
    text = _WIKILINK_RE.sub(_wikilink, text)

    out: List[str] = []
    region: List[str] = []
    verbatim = False

    def close_region() -> None:
        if region:
            # A verbatim region is a <table>, handed to Qt as HTML: a markdown
            # link written into one would show as its own literal text.
            out.append("\n".join(region) if verbatim
                       else _html_to_markdown(
                           _link_codes("\n".join(region), codes)))
            region.clear()

    for line in text.split("\n"):
        stripped = line.strip()
        if region:
            region.append(line)
            if _TABLE_END_RE.search(line) if verbatim else not stripped:
                close_region()
            continue
        if _LONE_IMG_LINE_RE.match(line):
            # Blank lines around it so the image can never be absorbed into an
            # adjacent paragraph — the whole point of promoting it to markdown.
            out.extend(["", _img_markdown(stripped), ""])
            continue
        if _HTML_BLOCK_START_RE.match(line):
            # A <table> is the one construct Qt imports properly, so it is
            # handed over untouched; everything else becomes markdown.
            verbatim = bool(_TABLE_RE.match(line))
            region.append(line)
            continue
        out.append(_link_codes(line, codes))
    close_region()
    return _BLANKS_RE.sub("\n\n", "\n".join(out))


def to_display_markdown(text: str,
                        *, share_codes: Optional[List[str]] = None) -> str:
    """Adapt raw wiki markdown for rendering in a QTextBrowser.

    Pass a list as *share_codes* to also make profile share codes clickable:
    each ``AMMCODE…`` found is appended to it and replaced by a truncated
    :data:`SHARE_CODE_SCHEME` link whose target is its index in that list, so
    the viewer can offer to import the code the reader clicked. Left out, the
    codes render as the plain text they are.

    Four adjustments, none of which touches the wiki itself:

    * HTML comments are dropped. GitHub hides them; Qt's importer keeps their
      text, so a commented-out screenshot showed up as stray ``-->`` prose.
    * ``[[Page]]`` / ``[[Label|Page]]`` wiki links become ordinary markdown
      links so they are clickable instead of showing as literal brackets.
    * An ``<img>`` tag alone on its line — how GitHub records a pasted
      screenshot — is rewritten as a native ``![alt](src)`` image. Left as
      HTML it reaches Qt's importer as an opaque fragment and gets welded to a
      neighbouring paragraph instead of standing on its own; dropping the
      tag's ``width``/``height`` also lets the viewer scale wide screenshots
      down rather than forcing horizontal scrolling.
    * Blocks of hand-written HTML — the ``<p align="center">`` header on this
      wiki's Home page — are converted to markdown for the same reason, with
      ``align="center"`` recorded as a :data:`CENTER_MARK` for the viewer.

    ``<table>`` is the single exception, passed through as HTML: Qt imports it
    into a real table, which markdown pipe syntax could not express anyway
    (the Home page's game list is four columns of ``<td>``).
    """
    return "".join(
        _rewrite_code_fence(chunk, share_codes) if is_code
        else _rewrite_prose(chunk, share_codes)
        for is_code, chunk in _split_fences(text)
    )
