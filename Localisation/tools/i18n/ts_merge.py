#!/usr/bin/env python3
"""Context-aware merge helpers shared by the machine-translation backends.

Qt identifies a message by (context, source, comment) — NOT by source alone.
The same English word appears in many contexts ("Save" is in 15 of ours, "Cancel"
in 43) and a translator may legitimately render it differently in each: a Save
button on the plugin list is not the Save on the theme editor.

Both backends used to key their "already translated" map on the source string
only, then write that one translation into every context carrying it. A refresh
therefore overwrote a translator's context-specific work with whichever variant
ElementTree happened to read last — silent data loss on 315 of our sources.

These helpers key on the full identity so existing work survives, while the
API still batches by unique TEXT (translating "Save" 15 times is pure waste).
"""
from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path

# A message's identity: (context name, source text). The optional <comment>
# (Qt's disambiguation) joins the key when present — two messages in one context
# with the same source are distinguished by it and nothing else.
Key = tuple[str, str]


def _key(ctx_name: str, msg: ET.Element) -> Key:
    src = msg.find("source")
    comment = msg.find("comment")
    text = (src.text or "") if src is not None else ""
    disambig = (comment.text or "") if comment is not None else ""
    return (ctx_name, f"{text}\x00{disambig}" if disambig else text)


def _iter_messages(root: ET.Element):
    """Yield (context_name, message) for every message, keeping the context."""
    for ctx in root.findall("context"):
        name_el = ctx.find("name")
        name = (name_el.text or "") if name_el is not None else ""
        for msg in ctx.findall("message"):
            yield name, msg


def read_existing(ts_path: Path) -> dict[Key, str]:
    """Map (context, source) -> finished translation from an existing .ts.

    Unfinished and empty entries are skipped: they carry no work to preserve.
    A missing file yields an empty map, so a brand-new language just works.
    """
    if not ts_path.is_file():
        return {}
    out: dict[Key, str] = {}
    try:
        root = ET.parse(ts_path).getroot()
    except ET.ParseError:
        return {}
    for ctx_name, msg in _iter_messages(root):
        tr = msg.find("translation")
        if tr is None or not tr.text or tr.get("type") == "unfinished":
            continue
        out[_key(ctx_name, msg)] = tr.text
    return out


def pending_texts(root: ET.Element, existing: dict[Key, str]) -> list[str]:
    """Unique source texts that still need translating, in first-seen order.

    A text is pending when ANY message carrying it lacks a translation — not
    when the text is unknown overall. "Save" translated in PluginView but not in
    ThemeEditor must still be sent, or the second context stays unfinished
    forever; keying this check on the text alone was why partially-translated
    strings never completed. One API call still covers every context that needs
    it, because the result is applied by text.
    """
    out: list[str] = []
    seen: set[str] = set()
    for ctx_name, msg in _iter_messages(root):
        src_el = msg.find("source")
        source = (src_el.text or "") if src_el is not None else ""
        if not source or source in seen:
            continue
        if _key(ctx_name, msg) not in existing:
            seen.add(source)
            out.append(source)
    return out


def apply_translations(root: ET.Element, existing: dict[Key, str],
                       fresh: dict[str, str]) -> tuple[int, int]:
    """Fill in every <translation> of *root* (a clone of the English base).

    Precedence is deliberate: an existing per-context translation always wins
    over a freshly machine-translated one for the same text, so a human's work
    is never replaced by the API's guess. Anything with neither is left
    unfinished, which makes Qt fall back to the English source at runtime.

    Returns (kept, translated) counts.
    """
    kept = translated = 0
    for ctx_name, msg in _iter_messages(root):
        src_el, tr_el = msg.find("source"), msg.find("translation")
        if src_el is None or tr_el is None:
            continue
        key = _key(ctx_name, msg)
        source = src_el.text or ""
        if key in existing:
            tr_el.text = existing[key]
            tr_el.attrib.pop("type", None)
            kept += 1
        elif source in fresh:
            tr_el.text = fresh[source]
            tr_el.attrib.pop("type", None)
            translated += 1
        else:
            tr_el.text = ""
            tr_el.set("type", "unfinished")
    return kept, translated


def write_ts(root: ET.Element, path: Path) -> None:
    """Serialise *root*, restoring the <!DOCTYPE TS> line ElementTree drops."""
    body = ET.tostring(root, encoding="unicode")
    with path.open("w", encoding="utf-8") as f:
        f.write('<?xml version="1.0" encoding="utf-8"?>\n<!DOCTYPE TS>\n')
        f.write(body)
        f.write("\n")
