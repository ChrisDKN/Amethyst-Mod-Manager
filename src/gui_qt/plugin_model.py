"""Plugin-tab model - QAbstractTableModel over PluginRow list.

Columns: Plugin Name, Flags, Lock, Priority, Index (checkbox painted into col 0
by the delegate). Toggling enable writes back via plugin_state.save.
"""

from __future__ import annotations

# Crash-proof diagnostic prints (Flatpak stdout can raise BrokenPipeError and
# kill worker threads). See Utils.app_log.safe_print.
from Utils.app_log import safe_print as print  # noqa: A004

from PySide6.QtCore import (
    Qt, QAbstractTableModel, QModelIndex, Signal, QT_TRANSLATE_NOOP,
)

from gui_qt.plugin_state import (
    PluginRow, save_plugins, compute_game_indexes,
    enforce_master_block, master_block_enabled, plugin_rank,
    movable_bounds, dependency_bounds,
)

COL_NAME = 0
COL_FLAGS = 1
COL_LOCK = 2
COL_PRIORITY = 3    # list-position counter (000, 001…), labelled "P"
COL_GAME_INDEX = 4  # MO2-style hex load index the game assigns (00, FE:000…)
COLUMNS = ["Plugin Name", "Flags", "", "P", "Index"]
# headerData() translates these at display time (self.tr(COLUMNS[i])); register
# the literals so lupdate extracts them under the PluginModel context. Must be
# explicit literal calls - lupdate can't see through a loop variable.
_COL_TR = (
    QT_TRANSLATE_NOOP("PluginModel", "Plugin Name"),
    QT_TRANSLATE_NOOP("PluginModel", "Flags"),
    QT_TRANSLATE_NOOP("PluginModel", "P"),
    QT_TRANSLATE_NOOP("PluginModel", "Index"),
)

RowRole = Qt.UserRole + 1      # the PluginRow
PFlagsRole = Qt.UserRole + 2   # int flag bitmask
PHighlightRole = Qt.UserRole + 3  # 0 none, 3 master(green), 2 anchor(orange), 1 higher, -1 lower
_ITEM_FLAGS = Qt.ItemIsEnabled | Qt.ItemIsSelectable


class PluginModel(QAbstractTableModel):
    # Emitted after the plugin order / enable state is persisted (reorder or
    # toggle). BSA load order follows plugin load order, so the window listens
    # to this to recompute BSA conflicts. See _save().
    order_changed = Signal()
    # plugins.txt write failed - the window surfaces a toast.
    save_failed = Signal(str)

    def __init__(self, rows: list[PluginRow] | None = None):
        super().__init__()
        # _rows is the DISPLAY list - with no column sort active it IS _natural
        # (same object); a sort derives a reordered copy. The natural order is
        # the load order, and plugins.txt / loadorder.txt are ALWAYS written
        # from it (see _save / natural_rows).
        self._natural: list[PluginRow] = rows or []
        self._rows: list[PluginRow] = self._natural
        # Active column sort ("name"/"flags"/"priority"/"index") + direction.
        self._sort_key: str | None = None
        self._sort_ascending: bool = True
        # Per-name natural-order caches (plugin filenames are unique): load
        # position for the P column, game index for the Index column. Both must
        # read the NATURAL order so a display sort doesn't renumber them.
        self._nat_pos: dict[str, int] = {}
        self._game_index_map: dict[str, str] = {}
        self._game = None
        self._profile = None
        self._locks: dict[str, bool] = {}     # plugin name (lower) → locked
        self._profile_dir = None
        # Cross-panel highlight: plugin names (lower) → code (2 anchor / 1 / -1).
        self._highlights: dict[str, int] = {}
        # plugin name (lower) → non-default userlist group (flags tooltip).
        self._ul_groups: dict[str, str] = {}
        # Engine parity: masters load before non-masters, so a drag/keyboard
        # move may not cross that boundary. Set from game.plugins_master_block.
        self._master_block = False

    def set_rows(self, rows, game=None, profile=None, profile_dir=None):
        self.beginResetModel()
        self._natural = rows
        self._game = game
        self._profile = profile
        self._profile_dir = profile_dir
        self._master_block = master_block_enabled(game)
        self._locks = {}
        self._highlights = {}
        self._refresh_natural_caches()
        self._rows = self._derive_display()
        if profile_dir is not None:
            try:
                from Utils.profiles.state import read_plugin_locks
                self._locks = read_plugin_locks(profile_dir) or {}
            except Exception:
                self._locks = {}
        self.endResetModel()

    # ---- column sorting ---------------------------------------------------
    def set_sort(self, key: str | None, ascending: bool = True) -> None:
        """Set (or clear with None) the active column sort and rebuild the
        display order. The natural (load) order is untouched."""
        key = key or None
        ascending = bool(ascending)
        if (key, ascending) == (self._sort_key, self._sort_ascending):
            return
        self._sort_key = key
        self._sort_ascending = ascending
        self._rebuild_display()

    def sort_state(self) -> tuple[str | None, bool]:
        return self._sort_key, self._sort_ascending

    @property
    def display_is_natural(self) -> bool:
        """True when the displayed order IS the load order - no sort, or the P
        column ascending. Drag-reorder is only meaningful in that state."""
        return self._rows is self._natural

    def natural_rows(self) -> list[PluginRow]:
        """Rows in natural load order. Any code that persists the load order or
        reads a plugin's neighbours MUST start from this, never the display
        order."""
        return self._natural

    def natural_index(self, name: str) -> int:
        """Load-order position of *name* (case-insensitive), or -1."""
        return self._nat_pos.get((name or "").lower(), -1)

    def set_natural_rows(self, rows: list[PluginRow]) -> None:
        """Replace the load order wholesale (LOOT sort) and re-derive the
        display. Locks/highlights are keyed by name, so they survive."""
        self.beginResetModel()
        self._natural = rows
        self._refresh_natural_caches()
        self._rows = self._derive_display()
        self.endResetModel()

    def _refresh_natural_caches(self) -> None:
        self._nat_pos = {r.name.lower(): i for i, r in enumerate(self._natural)}
        self._game_index_map = {
            r.name.lower(): v
            for r, v in zip(self._natural, compute_game_indexes(self._natural))}

    def _sort_ctx(self) -> dict:
        return {"positions": self._nat_pos, "indexes": self._game_index_map}

    def _derive_display(self) -> list[PluginRow]:
        from gui_qt.plugin_sort import build_display
        return build_display(self._natural, self._sort_key,
                             self._sort_ascending, self._sort_ctx())

    def _rebuild_display(self) -> None:
        """Re-derive the display list from the natural order + active sort.
        Uses layoutChanged with a persistent-index remap (by row identity) so
        selection/scroll follow the rows. No-op if the order is unchanged."""
        old = self._rows
        new = self._derive_display()
        if len(new) == len(old) and all(a is b for a, b in zip(new, old)):
            self._rows = new
            return
        self.layoutAboutToBeChanged.emit()
        old_persist = self.persistentIndexList()
        pos_by_id = {id(r): i for i, r in enumerate(new)}
        self._rows = new
        new_persist = []
        for idx in old_persist:
            r = old[idx.row()] if 0 <= idx.row() < len(old) else None
            row = pos_by_id.get(id(r), -1) if r is not None else -1
            new_persist.append(self.index(row, idx.column()) if row >= 0
                               else QModelIndex())
        self.changePersistentIndexList(old_persist, new_persist)
        self.layoutChanged.emit()

    def flags_changed(self) -> None:
        """Row flag bits were mutated in place (ESL scan / userlist refresh) -
        re-derive the display if the Flags column is the active sort."""
        self._resort_if_key("flags")

    def _resort_if_key(self, *keys: str) -> None:
        """Rebuild the display when the active sort depends on data that just
        changed (a toggle renumbers the game indexes, for instance)."""
        if self._sort_key in keys:
            self._rebuild_display()

    def is_locked(self, i: int) -> bool:
        return bool(self._locks.get(self._rows[i].name.lower(), False))

    def is_locked_name(self, name: str) -> bool:
        """Lock state by plugin name - for callers working in natural order,
        where the display row index doesn't apply."""
        return bool(self._locks.get((name or "").lower(), False))

    def set_userlist_groups(self, groups: dict[str, str]) -> None:
        """groups maps plugin name (lower) → non-default userlist group name.
        Feeds the Flags-column tooltip (Tk parity)."""
        self._ul_groups = dict(groups or {})

    def userlist_group(self, name: str) -> str | None:
        """Non-default userlist group for *name* (or None). Read by the delegate
        to append a 'Group: …' line to the userlist-dot tooltip."""
        return self._ul_groups.get(name.lower())

    def enabled_lower(self) -> set[str]:
        """Set of enabled plugin filenames (lowercase). Feeds the LOOT tooltip's
        requirement/incompatibility filtering."""
        return {r.name.lower() for r in self._rows if r.enabled}

    def all_lower(self) -> set[str]:
        """Set of ALL plugin filenames (lowercase), regardless of enabled state."""
        return {r.name.lower() for r in self._rows}

    def set_highlights(self, highlights: dict[str, int]) -> None:
        """highlights maps plugin name (lower) → code (3 master / 2 anchor /
        1 higher / -1 lower). Replaces the whole map and repaints."""
        self._highlights = dict(highlights or {})
        if self._rows:
            self.dataChanged.emit(self.index(0, 0),
                                  self.index(len(self._rows) - 1, COL_GAME_INDEX),
                                  [PHighlightRole])

    def toggle_lock(self, i: int):
        name = self._rows[i].name.lower()
        self._locks[name] = not self._locks.get(name, False)
        idx = self.index(i, COL_LOCK)
        self.dataChanged.emit(idx, idx, [])
        if self._profile_dir is not None:
            try:
                from Utils.profiles.state import write_plugin_locks
                write_plugin_locks(self._profile_dir, self._locks)
            except Exception as exc:
                print(f"[gui_qt] plugin locks save failed: {exc}", flush=True)

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self._rows)

    def columnCount(self, parent=QModelIndex()):
        return len(COLUMNS)

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if orientation == Qt.Horizontal and role == Qt.DisplayRole:
            # TkStyleHeader paints the label itself on sortable sections (elided
            # clear of the sort triangle) - it suppresses the native text for
            # the chrome pass, or the label is drawn twice.
            if getattr(self, "_suppress_header_text", False):
                return ""
            # "" (the lock column) stays empty; others are translated.
            return self.tr(COLUMNS[section]) if COLUMNS[section] else ""
        if (orientation == Qt.Horizontal and role == Qt.DecorationRole
                and section == COL_LOCK
                and not getattr(self, "_suppress_header_deco", False)):
            # The lock column has no text label; show a lock icon instead so
            # the header reads (matches the per-row lock glyph). TkStyleHeader
            # centres it and suppresses this during its chrome pass.
            from gui_qt.icons import icon
            return icon("lock.png", 14)
        return None

    def row(self, i: int) -> PluginRow:
        return self._rows[i]

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid():
            return None
        r = self._rows[index.row()]
        col = index.column()
        if role == RowRole:
            return r
        if role == PFlagsRole:
            return r.flags
        if role == PHighlightRole:
            return self._highlights.get(r.name.lower(), 0)
        if role == Qt.DisplayRole:
            if col == COL_NAME:
                return r.name
            if col == COL_PRIORITY:
                # The NATURAL load position - sorting by another column doesn't
                # renumber the load order (modlist parity).
                return f"{self._nat_pos.get(r.name.lower(), index.row()):03d}"
            if col == COL_GAME_INDEX:
                return self._game_index_map.get(r.name.lower(), "")
            return ""
        # Flags-column tooltips are handled per-icon by PluginDelegate.helpEvent
        # (Tk parity), not via a whole-cell Qt.ToolTipRole.
        return None

    def flags(self, index):
        if not index.isValid():
            return Qt.NoItemFlags
        return _ITEM_FLAGS

    def toggle(self, i: int):
        r = self._rows[i]
        if r.vanilla:
            return   # vanilla plugins are always-on; can't be disabled
        r.enabled = not r.enabled
        # Whole row: enabled state dims the text in every column.
        self.dataChanged.emit(self.index(i, 0),
                              self.index(i, len(COLUMNS) - 1),
                              [RowRole, Qt.DisplayRole])
        # Disabling/enabling a plugin renumbers every following plugin's game
        # index, so refresh the whole column (not just this row).
        self._refresh_game_indexes()
        self._save()

    def set_enabled(self, indices, enabled: bool):
        """Enable/disable the given rows (skips vanilla - always-on), persist +
        repaint. Mirrors toggle() for the context menu's Enable/Disable items."""
        changed = [i for i in indices
                   if 0 <= i < len(self._rows) and not self._rows[i].vanilla]
        if not changed:
            return
        for i in changed:
            self._rows[i].enabled = enabled
        lo, hi = min(changed), max(changed)
        self.dataChanged.emit(self.index(lo, 0),
                              self.index(hi, len(COLUMNS) - 1),
                              [RowRole, Qt.DisplayRole])
        self._refresh_game_indexes()
        self._save()

    def is_movable(self, i: int) -> bool:
        """A row may be dragged unless it's vanilla (pinned) or user-locked."""
        if not (0 <= i < len(self._rows)):
            return False
        if self._rows[i].vanilla:
            return False
        return not self.is_locked(i)

    def _first_movable(self) -> int:
        """Lowest row index a non-vanilla plugin may occupy (after the pinned
        vanilla block at the top)."""
        i = 0
        while i < len(self._rows) and self._rows[i].vanilla:
            i += 1
        return i

    def _clamp_dest(self, src: list[int], dest: int,
                    rows: list[PluginRow] | None = None) -> int:
        """Clamp an insert-before *dest* for the contiguous block *src*."""
        # Works on the "rest" list (rows minus the block) so every bound is a
        # plain insertion point. MO2's order: rank region, then dependencies.
        rows = self._rows if rows is None else rows
        first, last = src[0], src[-1]
        block = rows[first:last + 1]
        rest = rows[:first] + rows[last + 1:]
        d = dest if dest <= first else dest - len(block)
        d = max(0, min(d, len(rest)))
        ranks = {plugin_rank(r) for r in block}
        if self._master_block and len(ranks) == 1:
            lo, hi = movable_bounds(rest, ranks.pop(), True)
        else:
            # Mixed-rank block: let it land, then re-partition.
            lo, hi = movable_bounds(rest, 0, False)
        d = max(lo, min(d, hi))
        dlo, dhi = dependency_bounds(rest, block, moving_up=(d <= first))
        return max(dlo, min(d, dhi))

    def drop_bounds(self, src_rows: list[int]) -> "tuple[int, int]":
        """Legal (lo, hi) drop range in FULL-list coords, for the view."""
        # Dependency limits are direction-dependent, so evaluate both ends.
        src = sorted({i for i in src_rows if 0 <= i < len(self._rows)})
        if not src or src[-1] - src[0] != len(src) - 1:
            return self._first_movable(), len(self._rows)
        first, last = src[0], src[-1]
        span = len(src)
        lo = self._clamp_dest(src, 0)
        hi = self._clamp_dest(src, len(self._rows))
        # Back to full-list coords: rest indices past the block sit `span` lower.
        return (lo if lo <= first else lo + span,
                hi if hi <= first else hi + span)

    def move_rows(self, src_rows: list[int], dest: int) -> bool:
        """Move a contiguous block of movable rows so it lands before *dest*.
        Vanilla rows stay pinned; locked rows never move; the block may not
        cross a rank boundary. Persists on success."""
        if not self.display_is_natural:
            # Display rows aren't load-order rows under a column sort - the
            # view clears the sort before a drag, so this only guards callers
            # that forget to. (P ascending IS the load order, so it passes.)
            return False
        src = sorted(set(src_rows))
        if not src or any(not self.is_movable(i) for i in src):
            return False
        # Block must be contiguous for beginMoveRows.
        if src[-1] - src[0] != len(src) - 1:
            return False
        first, last = src[0], src[-1]
        # A nudge across a boundary clamps back to where the block already is,
        # which the no-op check turns into a silent False (Tk parity).
        insert_at = self._clamp_dest(src, dest)
        if insert_at == first:
            return False   # no-op / inside the moved span
        # A block spanning a rank boundary can't be one beginMoveRows plus a
        # partition, so move it and re-partition under a model reset.
        mixed = self._master_block and len({
            plugin_rank(self._rows[i]) for i in src}) != 1
        if mixed:
            self.beginResetModel()
            block = self._rows[first:last + 1]
            del self._rows[first:last + 1]
            self._rows[insert_at:insert_at] = block
            new_rows, _ = enforce_master_block(self._rows)
            self._rows[:] = new_rows
            self.endResetModel()
        else:
            # beginMoveRows wants the destination in FULL-list coordinates.
            dest_full = insert_at if insert_at <= first else insert_at + len(src)
            if not self.beginMoveRows(QModelIndex(), first, last,
                                      QModelIndex(), dest_full):
                return False
            block = self._rows[first:last + 1]
            del self._rows[first:last + 1]
            self._rows[insert_at:insert_at] = block
            self.endMoveRows()
        self._refresh_game_indexes()
        self._save()
        return True

    def set_priority(self, row: int, priority: int) -> bool:
        """Move one plugin to a constrained natural-order priority."""
        if not self.is_movable(row):
            return False
        plugin = self._rows[row]
        src = self.natural_index(plugin.name)
        if src < 0:
            return False
        target = max(0, min(len(self._natural) - 1, priority))
        if target == src:
            return False
        dest = target if target < src else target + 1
        if self.display_is_natural:
            return self.move_rows([src], dest)

        insert_at = self._clamp_dest([src], dest, self._natural)
        if insert_at == src:
            return False
        self._natural.pop(src)
        self._natural.insert(insert_at, plugin)
        self._refresh_natural_caches()
        self._rebuild_display()
        if self._rows:
            self.dataChanged.emit(
                self.index(0, COL_PRIORITY),
                self.index(len(self._rows) - 1, COL_GAME_INDEX),
                [Qt.DisplayRole],
            )
        self._save()
        return True

    def _refresh_game_indexes(self):
        """Recompute the cached MO2-style game indexes after an order/enabled
        change and repaint that column (data() reads the cache). Indexes follow
        the NATURAL load order, not the display sort."""
        self._nat_pos = {r.name.lower(): i for i, r in enumerate(self._natural)}
        self._game_index_map = {
            r.name.lower(): v
            for r, v in zip(self._natural, compute_game_indexes(self._natural))}
        if self._rows:
            self.dataChanged.emit(self.index(0, COL_PRIORITY),
                                  self.index(len(self._rows) - 1, COL_GAME_INDEX),
                                  [Qt.DisplayRole])
        self._resort_if_key("index")

    def _save(self):
        if self._game is not None and self._profile:
            try:
                save_plugins(self._game, self._profile, self._natural)
            except Exception as exc:
                print(f"[gui_qt] plugins.txt save failed: {exc}", flush=True)
                self.save_failed.emit(f"Plugins save failed: {exc}")
                return
            # loadorder.txt / plugins.txt are now on disk - let the window
            # recompute BSA conflicts (BSA winners follow plugin load order).
            self.order_changed.emit()
