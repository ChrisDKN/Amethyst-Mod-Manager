"""Small, dependency-free Nexus BBCode renderer.

Nexus descriptions are not consistent: some API responses contain HTML, some
contain BBCode, and older pages can contain both.  The renderer therefore keeps
real HTML intact while translating the BBCode constructs commonly emitted by
Nexus into the conservative HTML subset supported by ``QTextDocument``.
"""

from __future__ import annotations

import html
import re
from urllib.parse import urlsplit


_HTML_TAG = re.compile(
    r"</?(?:a|b|blockquote|br|center|code|div|em|font|h[1-6]|hr|i|img|li|ol|p|pre|s|span|"
    r"strong|sub|sup|table|tbody|td|th|thead|tr|u|ul)\b",
    re.IGNORECASE,
)
_BREAK_TOKEN = "NEXUSHTMLBREAKTOKEN"
_ENCODED_BREAK = re.compile(
    r"\\?(?:<|&lt;|&amp;lt;)\s*br\s*/?\s*(?:>|&gt;|&amp;gt;)",
    re.IGNORECASE,
)
_ORPHAN_BBCODE = re.compile(
    r"\[/?(?:b|i|u|s|sub|sup|color|font|size|center|left|right|justify|heading|section|"
    r"paragraph|h[1-6]|quote|spoiler|list|url|img|youtube|table|tr|td|"
    r"code|noparse|br|hr|line|\*)(?=[\s=\]])(?:[^\]]*)\]",
    re.IGNORECASE,
)


def _safe_url(value: str) -> str:
    """Return an escaped web URL, or an empty string for unsafe schemes."""
    value = html.unescape(value or "").strip().strip('"\'')
    if value.lower().startswith("www."):
        value = "https://" + value
    try:
        if urlsplit(value).scheme.lower() not in ("http", "https"):
            return ""
    except ValueError:
        return ""
    return html.escape(value, quote=True)


def _image_html(value: str, attributes: str = "") -> str:
    """Build a safe image tag, retaining Nexus alignment/size attributes."""
    target = _safe_url(value)
    if not target:
        return html.escape(html.unescape(value or ""))
    attrs = html.unescape(attributes or "")
    rendered: list[str] = []
    align = re.search(
        r"\balign\s*=\s*['\"]?(left|right|center)['\"]?", attrs,
        flags=re.IGNORECASE)
    if align and align.group(1).lower() in ("left", "right"):
        rendered.append(f'style="float:{align.group(1).lower()}"')
    number = r"\d+(?:\.\d+)?"
    dimensions = re.search(rf"=\s*['\"]?({number})\s*x\s*({number})", attrs,
                           re.IGNORECASE)
    if dimensions:
        rendered.extend(f'{key}="{max(1, min(float(value), 100000)):g}"'
                        for key, value in zip(("width", "height"), dimensions.groups()))
    else:
        for key in ("width", "height"):
            found = re.search(rf"\b{key}\s*=\s*['\"]?({number})", attrs,
                              flags=re.IGNORECASE)
            if found:
                rendered.append(f'{key}="{max(1, min(float(found.group(1)), 100000)):g}"')
    suffix = (" " + " ".join(rendered)) if rendered else ""
    image = f'<img src="{target}"{suffix}>'
    if align and align.group(1).lower() == "center":
        return f'<div align="center">{image}</div>'
    return image


def _replace_paired(text: str, tag: str, opening: str, closing: str) -> str:
    """Replace paired tags repeatedly so nested occurrences also resolve."""
    pattern = re.compile(
        rf"\[{re.escape(tag)}\](.*?)\[/{re.escape(tag)}\]",
        re.IGNORECASE | re.DOTALL,
    )
    for _ in range(20):
        updated = pattern.sub(lambda match: opening + match.group(1) + closing, text)
        if updated == text:
            break
        text = updated
    return text


def _replace_lists(text: str) -> str:
    pattern = re.compile(
        r"\[list(?:=([^\]]+))?\]((?:(?!\[list(?:=|\])).)*?)\[/list\]",
        re.IGNORECASE | re.DOTALL,
    )

    def repl(match: re.Match) -> str:
        style = html.unescape(match.group(1) or "").strip().strip('"\'')
        list_tag = "ol" if style in ("1", "a", "A", "i", "I") else "ul"
        list_type = f' type="{style}"' if style in (
            "1", "a", "A", "i", "I", "disc", "circle", "square") else ""
        body = re.sub(r"\[/\*\]", "", match.group(2), flags=re.IGNORECASE)
        parts = re.split(r"\[\*\]", body, flags=re.IGNORECASE)
        items = [part.strip() for part in parts if part.strip()]
        if not items:
            return ""
        return f"<{list_tag}{list_type}>" + "".join(
            f"<li>{item}</li>" for item in items) + f"</{list_tag}>"

    for _ in range(10):
        updated = pattern.sub(repl, text)
        if updated == text:
            break
        text = updated
    return text


def _remove_empty_list_items(text: str) -> str:
    """Drop list rows that contain no visible text or media.

    Some Nexus descriptions start a list with an item containing only another
    empty BBCode tag. It looks non-empty during the first list pass, but becomes
    ``<li><span ...></span></li>`` after formatting is resolved and Qt renders
    it as a stray bullet. Do this cleanup after every other conversion.
    """
    item_pattern = re.compile(r"<li\b[^>]*>((?:(?!<li\b).)*?)</li>",
                              re.IGNORECASE | re.DOTALL)

    def item(match: re.Match) -> str:
        body = match.group(1)
        # Images/rules are visible even though stripping tags yields no text.
        if re.search(r"<(?:img|hr)\b", body, re.IGNORECASE):
            return match.group(0)
        visible = re.sub(r"<[^>]*>", "", html.unescape(body))
        return match.group(0) if visible.strip() else ""

    text = item_pattern.sub(item, text)
    # Removing the only item can leave an empty container behind.
    return re.sub(r"<(ul|ol)\b[^>]*>\s*</\1>", "", text,
                  flags=re.IGNORECASE | re.DOTALL)


def _replace_spoilers(text: str, expanded: set[int]) -> str:
    """Render nested spoiler tags as stable in-document toggle links."""
    token_pattern = re.compile(
        r"\[spoiler(?:=([^\]]+))?\]|\[/spoiler\]", re.IGNORECASE)
    next_id = 0

    def parse(position: int, expect_close: bool):
        nonlocal next_id
        output: list[str] = []
        while True:
            match = token_pattern.search(text, position)
            if match is None:
                output.append(text[position:])
                return "".join(output), len(text), not expect_close
            output.append(text[position:match.start()])
            is_close = match.group(0).lower().startswith("[/")
            if is_close:
                if expect_close:
                    return "".join(output), match.end(), True
                output.append(match.group(0))
                position = match.end()
                continue

            spoiler_id = next_id
            next_id += 1
            body, end, closed = parse(match.end(), True)
            if not closed:
                # Keep malformed/unclosed source readable rather than dropping
                # the rest of the description.
                output.append(text[match.start():])
                return "".join(output), len(text), not expect_close
            title = html.escape(html.unescape(
                (match.group(1) or "Spoiler").strip('"\'')))
            href = f"nexus-spoiler:{spoiler_id}"
            if spoiler_id in expanded:
                output.append(
                    f'<blockquote><a href="{href}">▼ {title}</a><br>'
                    f"{body}</blockquote>")
            else:
                output.append(f'<p><a href="{href}">▶ {title}</a></p>')
            position = end

    rendered, _end, _closed = parse(0, False)
    return rendered


def nexus_bbcode_to_html(source: str, expanded_spoilers=None) -> str:
    """Convert a Nexus HTML/BBCode description into ``QTextBrowser`` HTML.

    Plain BBCode text is escaped before tags are introduced. Existing HTML is
    retained because a substantial number of Nexus API responses already use
    HTML. Script execution is not supported by QTextDocument, while link targets
    introduced here are restricted to HTTP(S).
    """
    source = (source or "").replace("\r\n", "\n").replace("\r", "\n")
    if not source:
        return ""
    expanded_spoilers = {int(value) for value in (expanded_spoilers or ())}
    # Code/noparse content must not be interpreted as either HTML or BBCode.
    code_blocks: list[str] = []

    def stash_code(match: re.Match) -> str:
        token = f"NEXUSCODEBLOCKTOKEN{len(code_blocks)}ENDTOKEN"
        content = html.escape(html.unescape(match.group(2)))
        code_blocks.append(
            f'<pre><code>{content}</code></pre>' if match.group(1).lower() == "code"
            else f'<span style="white-space:pre-wrap">{content}</span>')
        return token

    source = re.sub(
        r"\[(code|noparse)\](.*?)\[/\1\]",
        stash_code, source, flags=re.IGNORECASE | re.DOTALL)

    source = _ENCODED_BREAK.sub(_BREAK_TOKEN, source)
    contains_html = bool(_HTML_TAG.search(source))
    preserve_newlines = bool(_ORPHAN_BBCODE.search(source)) or not re.search(
        r"<(?:p|div|blockquote|h[1-6]|pre|table|ul|ol)\b", source, re.IGNORECASE)
    text = source if contains_html else html.escape(html.unescape(source), quote=False)

    # An image commonly sits inside a URL tag. Collapse that pair first so the
    # two independent replacements below cannot produce nested anchors.
    def linked_image(match: re.Match) -> str:
        target = _safe_url(match.group(1))
        image = _image_html(match.group(3), match.group(2))
        return f'<a href="{target}">{image}</a>' if target else image

    text = re.sub(
        r"\[url=([^\]]+)\]\s*\[img([^\]]*)\](.*?)\[/img\]\s*\[/url\]",
        linked_image, text, flags=re.IGNORECASE | re.DOTALL)

    def url_with_label(match: re.Match) -> str:
        target = _safe_url(match.group(1))
        label = match.group(2).strip() or target
        return f'<a href="{target}">{label}</a>' if target else label

    # Legacy Nexus descriptions sometimes mix a BBCode opener with an HTML or
    # pseudo-BBCode closer: ``[url=https://…]label</a>`` / ``[/a]``.
    text = re.sub(
        r"\[url=([^\]]+)\](.*?)(?:</a\s*>|\[/a\])", url_with_label, text,
        flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(
        r"\[url=([^\]]+)\](.*?)\[/url\]", url_with_label, text,
        flags=re.IGNORECASE | re.DOTALL)

    def bare_url(match: re.Match) -> str:
        label = match.group(1).strip()
        target = _safe_url(label)
        return f'<a href="{target}">{label}</a>' if target else label

    text = re.sub(
        r"\[url\](.*?)\[/url\]", bare_url, text,
        flags=re.IGNORECASE | re.DOTALL)

    def image_tag(match: re.Match) -> str:
        return _image_html(match.group(2), match.group(1))

    text = re.sub(
        r"\[img([^\]]*)\](.*?)\[/img\]", image_tag, text,
        flags=re.IGNORECASE | re.DOTALL)
    # Another legacy shape has no closing tag and ends the URL with a stray
    # quote/angle bracket: ``[img]https://…/image.png">``.
    text = re.sub(
        r"\[img([^\]]*)\]\s*(https?://[^\s<>\]\"']+)[\"']?\s*>?",
        image_tag, text, flags=re.IGNORECASE)

    def quote(match: re.Match) -> str:
        author = html.escape(html.unescape((match.group(1) or "").strip('"\'')))
        label = f"<b>{author} wrote:</b><br>" if author else "<b>Quote:</b><br>"
        return f"<blockquote>{label}{match.group(2)}</blockquote>"

    quote_pattern = re.compile(
        r"\[quote(?:=([^\]]+))?\](.*?)\[/quote\]",
        re.IGNORECASE | re.DOTALL)
    for _ in range(20):
        updated = quote_pattern.sub(quote, text)
        if updated == text:
            break
        text = updated

    text = _replace_spoilers(text, expanded_spoilers)

    text = _replace_lists(text)

    for tag, opening, closing in (
        ("b", "<b>", "</b>"), ("i", "<i>", "</i>"),
        ("u", "<u>", "</u>"), ("s", "<s>", "</s>"),
        ("sub", "<sub>", "</sub>"), ("sup", "<sup>", "</sup>"),
        ("center", '<div align="center">', "</div>"),
        ("left", '<div align="left">', "</div>"),
        ("right", '<div align="right">', "</div>"),
        ("justify", '<div align="justify">', "</div>"),
        ("heading", "<h2>", "</h2>"),
        ("section", "<h3>", "</h3>"),
        ("paragraph", "<p>", "</p>"),
        ("table", "<table>", "</table>"),
        ("tr", "<tr>", "</tr>"), ("td", "<td>", "</td>"),
    ):
        text = _replace_paired(text, tag, opening, closing)

    for level in range(1, 7):
        text = _replace_paired(text, f"h{level}", f"<h{level}>", f"</h{level}>")

    def formatting(match: re.Match) -> str:
        tag, value, body, closing = match.groups()
        tag, closing = tag.lower(), closing.lower()
        value = html.unescape(value).strip().strip('"\'')
        if tag != closing and {tag, closing} != {"size", "font"}:
            return match.group(0)
        if tag == "color":
            if re.fullmatch(r"#[0-9a-fA-F]{3,8}|[a-zA-Z]{3,20}", value):
                return f'<span style="color:{value}">{body}</span>'
        elif tag == "font":
            return f'<font face="{html.escape(value, quote=True)}">{body}</font>'
        else:
            size = re.fullmatch(r"(\d+(?:\.\d+)?)(px|pt)?", value, re.IGNORECASE)
            if size:
                amount = float(size.group(1))
                unit = (size.group(2) or "px").lower()
                if not size.group(2) and amount.is_integer() and 1 <= amount <= 7:
                    amount = (10, 13, 16, 18, 24, 32, 48)[int(amount) - 1]
                return f'<span style="font-size:{max(6, min(amount, 96)):g}{unit}">{body}</span>'
        return body

    formatting_pattern = re.compile(
        r"\[(color|size|font)=([^\]]+)\]"
        r"((?:(?!\[(?:color|size|font)=).)*?)\[/(color|size|font)\]",
        re.IGNORECASE | re.DOTALL)
    for _ in range(20):
        updated = formatting_pattern.sub(formatting, text)
        if updated == text:
            break
        text = updated
    # Strip orphan legacy size/font tags after the paired passes above.
    text = re.sub(r"\[/?(?:font|size)(?:=[^\]]+)?\]", "", text,
                  flags=re.IGNORECASE)

    def youtube(match: re.Match) -> str:
        value = html.unescape(match.group(1)).strip()
        target = value if value.startswith(("http://", "https://")) \
            else f"https://www.youtube.com/watch?v={value}"
        safe = _safe_url(target)
        return f'<a href="{safe}">Watch video</a>' if safe else "Video"

    text = re.sub(
        r"\[youtube\](.*?)\[/youtube\]", youtube, text,
        flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r"\[(?:hr|line)\s*/?\]", "<hr>", text, flags=re.IGNORECASE)
    text = re.sub(r"\[br\s*/?\]", "<br>", text, flags=re.IGNORECASE)
    text = text.replace(_BREAK_TOKEN, "<br>")

    # Finally discard only recognised BBCode tags that remain malformed or
    # unmatched. Their content stays visible; ordinary bracketed prose and
    # unknown tags are deliberately untouched.
    text = _ORPHAN_BBCODE.sub("", text)

    text = _remove_empty_list_items(text)

    if preserve_newlines:
        # A physical source newline next to an explicit HTML break is usually
        # formatting around the tag, not an additional requested blank line.
        text = re.sub(r"((?:<br>[ \t]*)+)\n", lambda match: match.group(1).rstrip(),
                      text, flags=re.IGNORECASE)
        text = re.sub(r"\n[ \t]*((?:<br>[ \t]*)+)",
                      lambda match: match.group(1).rstrip(), text,
                      flags=re.IGNORECASE)
        pieces = re.split(r"(<pre\b[^>]*>.*?</pre>|<code\b[^>]*>.*?</code>|<[^>]+>)",
                          text, flags=re.IGNORECASE | re.DOTALL)
        text = "".join(piece if piece.startswith("<") else piece.replace("\n", "<br>\n")
                       for piece in pieces)
        # Avoid double spacing around the block elements introduced above.
        block = r"(?:blockquote|div|h[1-6]|hr|li|ol|p|pre|table|tr|ul)"
        text = re.sub(rf"(<{block}\b[^>]*>)<br>\n", r"\1", text,
                      flags=re.IGNORECASE)
        text = re.sub(rf"<br>\n(</?{block}\b[^>]*>)", r"\1", text,
                      flags=re.IGNORECASE)

    for index, block in enumerate(code_blocks):
        text = text.replace(f"NEXUSCODEBLOCKTOKEN{index}ENDTOKEN", block)
    return text
