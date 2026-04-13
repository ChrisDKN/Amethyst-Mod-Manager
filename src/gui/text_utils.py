"""
Shared text utilities for GUI modules.

- ``truncate_text``   — pixel-aware text truncation with bounded cache
- ``build_tree_str``  — ASCII folder-tree from a flat path list
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import tkinter as tk


# ---------------------------------------------------------------------------
# Text truncation (cached, binary-search)
# ---------------------------------------------------------------------------

_truncate_cache: dict[tuple, str] = {}
_TRUNCATE_CACHE_MAX = 2000


def clear_truncate_cache() -> None:
    """Drop all cached truncation results (call after column resize, etc.)."""
    _truncate_cache.clear()


def truncate_text(
    widget: tk.Widget,
    text: str,
    font: tuple | str,
    max_px: int,
) -> str:
    """Return *text* truncated with '\u2026' so it fits within *max_px* pixels.

    Uses a binary search for the cut point and a module-level bounded cache
    to avoid repeated Tcl ``font measure`` calls during scroll/redraw.

    Parameters
    ----------
    widget : tk.Widget
        Any live Tk widget (needed for the ``font measure`` Tcl call).
    text : str
        The string to truncate.
    font : tuple | str
        A Tk font descriptor (e.g. ``("Segoe UI", 11)``).
    max_px : int
        Maximum allowed width in pixels.
    """
    key = (text, font if isinstance(font, str) else tuple(font), max_px)
    cached = _truncate_cache.get(key)
    if cached is not None:
        return cached

    if max_px <= 0 or not text:
        _truncate_cache[key] = text
        return text

    try:
        measure = widget.tk.call("font", "measure", font, text)
        if measure <= max_px:
            result = text
        else:
            ellipsis = "\u2026"
            ellipsis_w = widget.tk.call("font", "measure", font, ellipsis)
            lo, hi = 0, len(text)
            while lo < hi:
                mid = (lo + hi + 1) // 2
                if widget.tk.call("font", "measure", font, text[:mid]) + ellipsis_w <= max_px:
                    lo = mid
                else:
                    hi = mid - 1
            result = text[:lo] + ellipsis
    except Exception:
        # Fallback: rough character estimate when Tk is unavailable
        max_chars = max(1, (max_px - 12) // 7)
        result = (text[: max_chars - 1] + "\u2026") if len(text) > max_chars else text

    if len(_truncate_cache) >= _TRUNCATE_CACHE_MAX:
        evict = len(_truncate_cache) - _TRUNCATE_CACHE_MAX // 2
        for k in list(_truncate_cache)[:evict]:
            del _truncate_cache[k]
    _truncate_cache[key] = result
    return result


def truncate_text_tk_call(
    tk_call,
    text: str,
    font,
    max_px: int,
) -> str:
    """Same as :func:`truncate_text` but accepts a raw ``tk.call`` callable.

    Useful when the caller already has ``widget.tk.call`` cached for speed.
    """
    key = (text, str(font), max_px)
    cached = _truncate_cache.get(key)
    if cached is not None:
        return cached

    if max_px <= 0 or not text:
        _truncate_cache[key] = text
        return text

    if tk_call("font", "measure", font, text) <= max_px:
        _truncate_cache[key] = text
        return text

    ellipsis = "\u2026"
    ellipsis_w = tk_call("font", "measure", font, ellipsis)
    lo, hi = 0, len(text)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if tk_call("font", "measure", font, text[:mid]) + ellipsis_w <= max_px:
            lo = mid
        else:
            hi = mid - 1
    result = text[:lo] + ellipsis

    if len(_truncate_cache) >= _TRUNCATE_CACHE_MAX:
        evict = len(_truncate_cache) - _TRUNCATE_CACHE_MAX // 2
        for k in list(_truncate_cache)[:evict]:
            del _truncate_cache[k]
    _truncate_cache[key] = result
    return result


def truncate_text_font(
    text: str,
    max_px: int,
    font: "tk.font.Font",
) -> str:
    """Same as :func:`truncate_text` but uses a ``tkfont.Font`` object directly."""
    key = (text, str(font), max_px)
    cached = _truncate_cache.get(key)
    if cached is not None:
        return cached

    if max_px <= 0 or not text:
        _truncate_cache[key] = text
        return text

    if font.measure(text) <= max_px:
        _truncate_cache[key] = text
        return text

    ellipsis = "\u2026"
    ellipsis_w = font.measure(ellipsis)
    lo, hi = 0, len(text)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if font.measure(text[:mid]) + ellipsis_w <= max_px:
            lo = mid
        else:
            hi = mid - 1
    result = text[:lo] + ellipsis

    if len(_truncate_cache) >= _TRUNCATE_CACHE_MAX:
        evict = len(_truncate_cache) - _TRUNCATE_CACHE_MAX // 2
        for k in list(_truncate_cache)[:evict]:
            del _truncate_cache[k]
    _truncate_cache[key] = result
    return result


# ---------------------------------------------------------------------------
# ASCII folder tree
# ---------------------------------------------------------------------------

def build_tree_str(paths: list[str]) -> str:
    """Convert a flat list of slash-separated paths into an ASCII folder tree."""
    root: dict = {}
    for path in sorted(paths):
        node = root
        for part in path.split("/"):
            node = node.setdefault(part, {})

    lines: list[str] = []

    def _walk(node: dict, prefix: str):
        items = sorted(node.keys())
        for i, name in enumerate(items):
            is_last = (i == len(items) - 1)
            lines.append(f"{prefix}{'\u2514\u2500\u2500 ' if is_last else '\u251c\u2500\u2500 '}{name}")
            child = node[name]
            if child:
                _walk(child, prefix + ("    " if is_last else "\u2502   "))

    _walk(root, "")
    return "\n".join(lines) if lines else "(no files)"
