"""Pure column-sort helpers for the Qt modlist - no Qt imports, fully
headless-testable.

Ports the Tk sort semantics (gui/modlist_panel.py - REFERENCE, do not modify):
separators never move, mods are sorted within their separator group, and the
special reverse-priority mode ("priority" ascending, 0 at top) inverts the
whole display: Root Folder on top, user groups reversed (lowest priority
first), a divider row, the ungrouped float (highest-priority mods without a
separator), then Overwrite at the bottom.

Unlike Tk (which keeps _entries natural and inverts it physically only during
a drag), the Qt model derives a persistent *display list* from the natural
list. In reverse mode that display list contains a real divider ModEntry
(DIVIDER_NAME) at all times, so the "bottom but ungrouped" drop slot is always
reachable and the divider never pops in/out.
"""

from __future__ import annotations

from Utils.modlist import ModEntry
from Utils.filegraph_constants import OVERWRITE_NAME, ROOT_FOLDER_NAME

# The reverse-mode divider between the last user group and the ungrouped
# float. UI-only: lives in the display list, never in the natural list, and is
# stripped by name on save as a belt-and-braces guard (Tk BOUNDARY_NAME).
DIVIDER_NAME = "__Ungrouped_Boundary__"

# Sortable-column keys (match the Tk _sort_column strings; persisted to ini).
SORT_KEYS = ("name", "category", "flags", "conflicts", "installed",
             "version", "author", "priority", "size")


def make_divider() -> ModEntry:
    return ModEntry(DIVIDER_NAME, True, True, True)


def is_reverse(key: str | None, ascending: bool) -> bool:
    return key == "priority" and ascending


# ---------------------------------------------------------------------------
# Grouping
# ---------------------------------------------------------------------------
def split_groups(entries: list[ModEntry]) -> list[tuple[ModEntry | None,
                                                        list[ModEntry]]]:
    """Split entries into (separator, [mods]) groups in order. Mods before the
    first separator form a leading (None, mods) group (Tk parity)."""
    groups: list[tuple[ModEntry | None, list[ModEntry]]] = []
    cur_sep: ModEntry | None = None
    cur_mods: list[ModEntry] = []
    for e in entries:
        if e.is_separator:
            if cur_sep is not None or cur_mods:
                groups.append((cur_sep, cur_mods))
            cur_sep = e
            cur_mods = []
        else:
            cur_mods.append(e)
    if cur_sep is not None or cur_mods:
        groups.append((cur_sep, cur_mods))
    return groups


# ---------------------------------------------------------------------------
# Per-column sort keys (Tk _sort_key_fn)
# ---------------------------------------------------------------------------
# Qt display conflict codes → Tk order: partial, loses, wins, full, none.
_CONFLICT_ORDER = {2: 0, -1: 1, 1: 2, 3: 3, 0: 4}


def sort_key_fn(key: str, ctx: dict):
    """Key function over ModEntry for column *key*. *ctx* holds the per-name
    data dicts: categories / versions / installed / size_bytes / flags
    (effective FLAG_* bits incl. overlays) / conflicts (display codes)."""
    if key == "name":
        return lambda e: e.name.lower()

    if key == "category":
        cats = ctx.get("categories") or {}
        # Missing category sorts last (high Unicode sentinel - Tk parity).
        return lambda e: (cats.get(e.name, "") or "￿").lower()

    if key == "author":
        auths = ctx.get("authors") or {}
        # Missing author sorts last, like category.
        return lambda e: (auths.get(e.name, "") or "￿").lower()

    if key == "installed":
        inst = ctx.get("installed") or {}

        def _installed_key(e):
            s = inst.get(e.name, "")
            # ISO "YYYY-MM-DD" strings sort identically to datetimes; mods
            # without a date sort last.
            return (0, s) if s else (1, "")
        return _installed_key

    if key == "flags":
        from gui_qt.modlist_data import (
            FLAG_MISSING_REQS, FLAG_UPDATE, FLAG_MODIO_UPDATE, FLAG_ROOT,
            FLAG_ROOT_RULE, FLAG_MODIFIED_MF, FLAG_PRERTX,
            FLAG_COLLECTION_BUNDLED, FLAG_COLLECTION_PATCHED, FLAG_ENDORSED,
            FLAG_THUNDERSTORE_UPDATE,
        )
        flags = ctx.get("flags") or {}

        def _flags_key(e):
            bits = flags.get(e.name, 0)
            score = 0
            if bits & FLAG_MISSING_REQS:
                score |= 128
            if e.locked:
                score |= 64
            if bits & (FLAG_UPDATE | FLAG_MODIO_UPDATE
                       | FLAG_THUNDERSTORE_UPDATE):
                score |= 32
            if bits & (FLAG_ROOT | FLAG_ROOT_RULE):
                score |= 16
            if bits & FLAG_MODIFIED_MF:
                score |= 8
            if bits & FLAG_PRERTX:
                score |= 4
            if bits & (FLAG_COLLECTION_BUNDLED | FLAG_COLLECTION_PATCHED):
                score |= 2
            if bits & FLAG_ENDORSED:
                score |= 1
            return -score   # flagged mods sort first when ascending
        return _flags_key

    if key == "conflicts":
        conf = ctx.get("conflicts") or {}
        return lambda e: _CONFLICT_ORDER.get(conf.get(e.name, 0), 4)

    if key == "version":
        vers = ctx.get("versions") or {}

        def _version_key(e):
            v = vers.get(e.name, "")
            if not v:
                return (1, ())   # missing version sorts last
            parts: list = []
            for tok in v.replace("-", ".").split("."):
                try:
                    parts.append((0, int(tok)))
                except ValueError:
                    parts.append((1, tok.lower()))
            return (0, tuple(parts))
        return _version_key

    if key == "size":
        sizes = ctx.get("size_bytes") or {}
        return lambda e: sizes.get(e.name, 0)

    return lambda e: 0


# ---------------------------------------------------------------------------
# Display-order construction (Tk _apply_column_sort)
# ---------------------------------------------------------------------------
def build_display(natural: list[ModEntry], key: str | None, ascending: bool,
                  ctx: dict, divider: ModEntry | None = None,
                  flatten_groups: bool = False
                  ) -> list[ModEntry]:
    """Derive the display order from the natural order. Returns a NEW list
    holding the SAME entry objects (plus the divider in reverse mode).

    When *flatten_groups* is set (the "hide separators" filter is active), a
    plain column sort ignores separator boundaries and orders every mod as one
    flat list - otherwise mods only sort within their own separator group and
    still cluster under the (now-hidden) separator, which reads as broken. The
    separators are appended at the end (hidden by the filter anyway) so the
    natural round-trip and boundary handling stay intact. The special
    reverse-priority mode is unaffected - its grouping is intrinsic."""
    if not key:
        return list(natural)

    if flatten_groups and key != "priority":
        key_fn = sort_key_fn(key, ctx)
        mods = [e for e in natural if not e.is_separator]
        seps = [e for e in natural if e.is_separator]
        return sorted(mods, key=key_fn, reverse=not ascending) + seps

    groups = split_groups(natural)

    if key == "priority":
        if not ascending:
            # Priority-descending is the natural order (unreachable via the
            # header's 2-click toggle, but harmless).
            return list(natural)
        ow = rf = None
        user: list[tuple[ModEntry | None, list[ModEntry]]] = []
        for g in groups:
            sep, _mods = g
            if sep is not None and sep.name == OVERWRITE_NAME:
                ow = g
            elif sep is not None and sep.name == ROOT_FOLDER_NAME:
                rf = g
            else:
                user.append(g)
        # Ungrouped float = mods in Overwrite's group (above the first user
        # separator in natural order = highest priority, no separator).
        floaters = ow[1] if ow else []
        out: list[ModEntry] = []
        if rf is not None:
            out.append(rf[0])
            out.extend(reversed(rf[1]))
        for sep, mods in reversed(user):
            if sep is not None:
                out.append(sep)
            out.extend(reversed(mods))
        # Divider between the last user group and the float - shown whenever
        # user separators exist, even with an empty float, so the slot is
        # always visible/reachable (Tk static-boundary-always-on).
        if user:
            out.append(divider if divider is not None else make_divider())
        out.extend(reversed(floaters))
        if ow is not None:
            out.append(ow[0])
        return out

    key_fn = sort_key_fn(key, ctx)
    out = []
    for sep, mods in groups:
        if sep is not None:
            out.append(sep)
        out.extend(sorted(mods, key=key_fn, reverse=not ascending))
    return out


def uninvert_display(display: list[ModEntry]) -> list[ModEntry]:
    """Convert a reverse-mode display list back to natural order (Tk
    _uninvert_entries_order). The divider entry is dropped; its group becomes
    the ungrouped float between Overwrite and the first user group."""
    groups = split_groups(display)
    ow = rf = None
    middle: list[tuple[ModEntry | None, list[ModEntry]]] = []
    for g in groups:
        sep, _mods = g
        if sep is not None and sep.name == OVERWRITE_NAME:
            ow = g
        elif sep is not None and sep.name == ROOT_FOLDER_NAME:
            rf = g
        else:
            middle.append(g)

    # Peel the divider-headed group out as the float (dropping the divider).
    ungrouped: list[ModEntry] = []
    for g in list(middle):
        sep, mods = g
        if sep is not None and sep.name == DIVIDER_NAME:
            ungrouped = mods
            middle.remove(g)
            break

    # With no user separators the ungrouped mods live in Root's group (first
    # separator in inverted-visual order) - promote them to a separator-less
    # group so they reverse between OW and Root.
    if rf is not None and rf[1]:
        middle.append((None, rf[1]))
        rf = (rf[0], [])

    new_groups = (([ow] if ow is not None else [])
                  + ([(None, ungrouped)] if ungrouped else [])
                  + list(reversed(middle))
                  + ([rf] if rf is not None else []))

    out: list[ModEntry] = []
    for sep, mods in new_groups:
        if sep is not None:
            out.append(sep)
        out.extend(reversed(mods))
    return out


# ---------------------------------------------------------------------------
# Reverse-mode drop resolution (Tk _on_mouse_drag inverted branch)
# ---------------------------------------------------------------------------
def resolve_reverse_drop(entries: list[ModEntry], slot: int,
                         src: set[int], full_block: bool,
                         hidden: set[int] | frozenset = frozenset()) -> int:
    """Resolve a drop *slot* (insert-before display row, pre-removal) into the
    actual pre-removal insert position, applying the reverse-mode semantics:

    - below == Overwrite (or past the end) → after it (uninverts to highest
      priority).
    - below == divider → before it (last mod of the group above; keeps the
      slot reachable).
    - below == FIRST user separator (right under Root) and NOT a full
      separator-block drag → after it, joining its group (the #165
      "jumps to priority 0" guard).
    - below == any other separator → before it (bottom of the group that ends
      there).
    - Top clamp: a lone mod / multi-selection can never land above the first
      user separator (the Root gap uninverts to the very top). Full separator
      blocks are exempt - dropped above the first user separator they become
      the new lowest-priority peer group.

    *hidden* = display rows currently hidden (collapsed blocks); the entry
    "below" the slot is the next non-dragged, non-hidden row (Tk resolves
    against the visible list).
    """
    n = len(entries)
    slot = max(0, min(slot, n))
    below = next((i for i in range(slot, n)
                  if i not in src and i not in hidden), None)

    rf_idx = next((i for i, e in enumerate(entries)
                   if e.is_separator and e.name == ROOT_FOLDER_NAME), None)
    first_user_sep = None
    if (rf_idx is not None and rf_idx + 1 < n
            and entries[rf_idx + 1].is_separator
            and entries[rf_idx + 1].name not in (OVERWRITE_NAME,
                                                 DIVIDER_NAME)):
        first_user_sep = rf_idx + 1

    if below is None:
        ins = n
    else:
        e = entries[below]
        if e.is_separator:
            if e.name == OVERWRITE_NAME:
                ins = below + 1
            elif e.name == DIVIDER_NAME:
                ins = below
            elif not full_block and below == first_user_sep:
                ins = below + 1
            else:
                ins = below
        else:
            ins = below

    if not full_block and first_user_sep is not None:
        ins = max(ins, first_user_sep + 1)
    return ins


def insert_separator_display(display: list[ModEntry], ref_row: int,
                             above: bool, sep: ModEntry) -> list[ModEntry]:
    """Insert *sep* next to display row *ref_row* in reverse mode and return
    the resulting NATURAL order (Tk _add_separator_inverted: resolve in
    display space, then uninvert). A separator ref anchors to its whole block
    so the new separator never splits header from mods."""
    disp = list(display)
    ref = disp[ref_row]
    if ref.is_separator:
        end = ref_row + 1
        while end < len(disp) and not disp[end].is_separator:
            end += 1
        slot = ref_row if above else end
    else:
        slot = ref_row if above else ref_row + 1
    disp.insert(slot, sep)
    return uninvert_display(disp)
