"""Pure column-sort helpers for the Qt plugins panel - no Qt imports, fully
headless-testable.

Unlike the modlist there are no separators to anchor, so a column sort is a
flat reorder of the DISPLAY list only. The natural order is the load order that
plugins.txt / loadorder.txt records and a sort never touches it: the model keeps
both lists and writes from the natural one (see PluginModel.natural_rows).

Sorts are stable, so ties keep their load-order sequence.
"""

from __future__ import annotations

from gui_qt.plugin_state import (
    PF_MISSING, PF_LATE, PF_VMM, PF_ESL, PF_LOOT, PF_DIRTY, PF_TAGS,
    PF_USERLIST, PF_UL_CYCLE,
)

# Sortable-column keys (persisted to the ini by column NAME via column_state).
SORT_KEYS = ("name", "flags", "priority", "index")

# Flag weights, most-problematic first - ascending puts flagged plugins on top
# (modlist parity).
_FLAG_WEIGHTS = (
    (PF_MISSING, 256), (PF_UL_CYCLE, 128), (PF_LATE, 64), (PF_VMM, 32),
    (PF_DIRTY, 16), (PF_LOOT, 8), (PF_TAGS, 4), (PF_USERLIST, 2), (PF_ESL, 1),
)


def flags_score(bits: int) -> int:
    score = 0
    for bit, weight in _FLAG_WEIGHTS:
        if bits & bit:
            score |= weight
    return score


def index_key(value: str):
    """Sort key for a game-index cell: normal slots first (by hex value), then
    light "FE:xxx" slots, then disabled plugins (no index) last."""
    if not value:
        return (2, 0)
    try:
        if ":" in value:
            return (1, int(value.split(":", 1)[1], 16))
        return (0, int(value, 16))
    except ValueError:
        return (2, 0)


def sort_key_fn(key: str, ctx: dict):
    """Key function over PluginRow for column *key*. *ctx* holds the per-name
    lookups: positions (natural load-order index) and indexes (game index
    strings), both keyed by lowercase plugin name."""
    if key == "name":
        return lambda r: r.name.lower()

    if key == "flags":
        # Negated so ascending = most-flagged first.
        return lambda r: -flags_score(r.flags)

    if key == "priority":
        pos = ctx.get("positions") or {}
        return lambda r: pos.get(r.name.lower(), 0)

    if key == "index":
        idx = ctx.get("indexes") or {}
        return lambda r: index_key(idx.get(r.name.lower(), ""))

    return lambda r: 0


def build_display(natural: list, key: str | None, ascending: bool,
                  ctx: dict) -> list:
    """Derive the display order from the natural order. Returns a NEW list
    holding the SAME PluginRow objects - or the natural list ITSELF when the
    result would be the load order anyway (no sort, or P ascending). Callers
    rely on that identity to tell "display == load order" apart, which is what
    keeps drag-reorder available under a P-ascending sort."""
    if not key or (key == "priority" and ascending):
        return natural
    return sorted(natural, key=sort_key_fn(key, ctx), reverse=not ascending)
