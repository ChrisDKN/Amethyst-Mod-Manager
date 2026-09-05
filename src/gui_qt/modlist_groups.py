"""Group operations shared by modlist menus, ordering, and rendering."""

from copy import deepcopy

from Utils.mods import groups as grouping
from gui_qt.modlist_sort import build_display, uninvert_display, resolve_reverse_drop


def summary_code(codes):
    if 2 in codes or (1 in codes and -1 in codes):
        return 2
    for code in (3, 1, -1):
        if code in codes:
            return code
    return 0


class ModGrouping:
    def _init_groups(self):
        self._mod_groups = {}
        self._saved_mod_groups = {}
        self._group_owners = {}
        self._group_row_map = None
        self._group_summary_cache = {}
        self._group_repainting = False
        self._group_recovery_needed = False
        self.dataChanged.connect(self._group_data_changed)

    def _index_groups(self):
        self._group_owners = grouping.owners(self._mod_groups)
        self._group_row_map = None
        self._group_summary_cache.clear()
        self._sep_hl_cache.clear()

    def _group_data_changed(self, *_args):
        if self._group_repainting:
            return
        self._group_summary_cache.clear()
        if not self._mod_groups:
            return
        rows = [rows[0] for rows in self._group_rows().values() if rows]
        if rows:
            self._group_repainting = True
            try:
                self.dataChanged.emit(self.index(min(rows), 0),
                                      self.index(max(rows), self.columnCount() - 1))
            finally:
                self._group_repainting = False

    def group_leader(self, name):
        return self._group_owners.get(name)

    def is_group_leader(self, name):
        return name in self._mod_groups

    def is_group_collapsed(self, name):
        return self._mod_groups.get(name, {}).get("collapsed", False)

    def _group_rows(self):
        if self._group_row_map is None:
            self._group_row_map = {leader: [] for leader in self._mod_groups}
            for row, entry in enumerate(self._entries):
                leader = self.group_leader(entry.name)
                if leader:
                    self._group_row_map[leader].append(row)
        return self._group_row_map

    def group_rows(self, name):
        return self._group_rows().get(self.group_leader(name), [])

    def group_summary(self, name):
        if name not in self._group_summary_cache:
            bits, loose, bsa, uuid = self.sep_block_summary(self.group_rows(name))
            self._group_summary_cache[name] = (
                bits, summary_code(loose), summary_code(bsa), summary_code(uuid))
        return self._group_summary_cache[name]

    def _save_groups(self):
        if self._mod_groups != self._saved_mod_groups and self.modlist_path:
            from Utils.profiles.state import write_mod_groups
            write_mod_groups(self.modlist_path.parent, self._mod_groups)
        self._saved_mod_groups = deepcopy(self._mod_groups)

    def _publish_group_edit(self, entries, groups):
        reset = len(entries) != len(self._natural)
        if reset:
            self.beginResetModel()
        self._natural, self._mod_groups = list(entries), groups
        self._index_groups()
        if reset:
            self._entries = self._derive_display()
            self.endResetModel()
        else:
            self._rebuild_display()

    def _commit_group_edit(self, entries, groups):
        groups = grouping.normalize_groups(groups, entries)
        entries = grouping.prioritize_leaders(entries, groups)
        if entries == self._natural and groups == self._mod_groups:
            return False
        old_entries, old_groups = self._natural, self._mod_groups
        old_names = self._mod_name_order()
        self._group_recovery_needed = False
        self._publish_group_edit(entries, groups)
        reordered = old_entries != self._natural
        reported = False
        try:
            if reordered:
                new_names = self._mod_name_order()
                old_positions = {n: i for i, n in enumerate(old_names)}
                moved = [n for i, n in enumerate(new_names) if old_positions.get(n) != i]
                ctx = self._move_ctx(old_names, new_names, moved)
                if not self.save(edit_ctx=("move", *ctx) if ctx else None):
                    reported = True
                    raise OSError("Could not save the group order")
            else:
                self._save_groups()
        except Exception as exc:
            if not self._group_recovery_needed:
                self._publish_group_edit(old_entries, old_groups)
            if not reported:
                self.save_failed.emit(str(exc))
            self.groups_changed.emit()
            return False
        self.groups_changed.emit()
        return True

    def group_with(self, names, leader):
        return self._commit_group_edit(*grouping.group_with(
            self._natural, self._mod_groups, names, leader, self.reverse_mode_active))

    def change_group_leader(self, leader, new):
        return self._commit_group_edit(self._natural,
                                      grouping.promote(self._mod_groups, leader, new))

    def ungroup_mods(self, names):
        return self._commit_group_edit(*grouping.ungroup(
            self._natural, self._mod_groups, names, self.reverse_mode_active))

    def toggle_group(self, leader):
        groups = deepcopy(self._mod_groups)
        if leader not in groups:
            return False
        groups[leader]["collapsed"] = not groups[leader]["collapsed"]
        return self._commit_group_edit(self._natural, groups)

    def set_groups_collapsed(self, collapsed):
        groups = deepcopy(self._mod_groups)
        for data in groups.values():
            data["collapsed"] = collapsed
        self._commit_group_edit(self._natural, groups)

    def expand_group_selection(self, rows):
        names = grouping.expand_leaders(
            [self.entry(r).name for r in rows], self._mod_groups)
        return [r for r, e in enumerate(self._entries) if e.name in names]

    def group_drop_slot(self, slot, rows):
        if 0 <= slot < len(self._entries):
            target = self.entry(slot).name
            leader = self.group_leader(target)
            if leader and leader != target:
                names = {self.entry(r).name for r in rows}
                if leader in names or any(self.group_leader(n) != leader for n in names):
                    return self.group_rows(leader)[-1] + 1
        return slot

    def _move_group_rows(self, rows, slot, hidden=frozenset()):
        from gui_qt.modlist_model import _PINNED_NAMES
        rows = self.expand_group_selection(rows)
        names = {self.entry(r).name for r in rows}
        if not names or names.intersection(_PINNED_NAMES):
            return False
        reverse = self.reverse_mode_active
        base = build_display(self._natural, "priority" if reverse else None,
                             True, {}, divider=self._divider)
        base_pos = {e.name: i for i, e in enumerate(base)}
        slot = max(0, min(slot, len(self._entries)))
        source_owners = {self.group_leader(n) for n in names}
        retain = next(iter(source_owners)) if len(source_owners) == 1 else None
        if retain in names:
            retain = None
        if retain:
            members = self.group_rows(retain)
            if not members[0] < slot <= members[-1] + 1:
                retain = None
        if retain:
            members = self.group_rows(retain)
            base_rows = [base_pos[self.entry(r).name] for r in members]
            if slot == members[0] + 1:
                at = min(base_rows)
            elif slot == members[-1] + 1:
                at = max(base_rows) + 1
            else:
                at = base_pos[self.entry(slot).name]
        elif slot < len(self._entries):
            target = self.entry(slot).name
            leader = self.group_leader(target)
            if leader:
                block_rows = [base_pos[self.entry(r).name] for r in self.group_rows(leader)]
                at = min(block_rows) if target == leader else max(block_rows) + 1
            else:
                at = base_pos[target]
        else:
            at = len(base)
        if reverse and not retain:
            src = {base_pos[n] for n in names}
            first = rows[0]
            full_sep = (self.entry(first).is_separator and len(rows) > 1
                        and set(rows) == {first, *self.sep_block_rows(first)})
            hidden_base = {base_pos[self.entry(r).name] for r in hidden}
            at = resolve_reverse_drop(base, at, src, full_sep, hidden=hidden_base)
            at = grouping.boundary_slot(base, self._mod_groups, at)
        lo, hi = self._movable_span()
        if not reverse:
            at = max(lo, min(at, hi))
        moved = [e for e in base if e.name in names]
        if any(e.locked and not e.is_separator for e in moved):
            return False
        insert = at - sum(e.name in names for e in base[:at])
        rest = [e for e in base if e.name not in names]
        rest[insert:insert] = moved
        natural = uninvert_display(rest) if reverse else rest
        groups = deepcopy(self._mod_groups)
        for leader, data in groups.items():
            if leader not in names and leader != retain:
                data["members"] = [n for n in data["members"] if n not in names]
        return self._commit_group_edit(natural, groups)

    def move_group_to_separator(self, rows, separator):
        names = [self.entry(r).name for r in rows]
        slot = next((i + 1 for i, e in enumerate(self._natural)
                     if e.name == separator and e.is_separator), None)
        if slot is None:
            return False
        return self._commit_group_edit(*grouping.relocate(
            self._natural, self._mod_groups, names, slot))

    def set_group_priority(self, row, priority):
        entry = self.entry(row)
        moving = grouping.expand_leaders([entry.name], self._mod_groups)
        mods = [e for e in self._natural if not e.is_separator]
        target = max(0, min(len(mods) - 1, len(mods) - 1 - priority))
        source = mods.index(entry)
        if source == target:
            return
        block = [e for e in mods if e.name in moving]
        start = max(0, min(len(mods) - len(block), target - block.index(entry)))
        others = [e for e in self._natural if e.name not in moving]
        other_mods = [e for e in others if not e.is_separator]
        if start < len(other_mods):
            anchor = other_mods[start]
            slot = self._natural.index(anchor)
        else:
            slot = len(self._natural) - 1
        leader = self.group_leader(entry.name)
        retain = None
        if leader and leader != entry.name:
            positions = [i for i, e in enumerate(self._natural)
                         if self.group_leader(e.name) == leader]
            if min(positions) <= slot <= max(positions) + 1:
                retain = leader
        self._commit_group_edit(*grouping.relocate(
            self._natural, self._mod_groups, moving, slot, retain=retain))

    def group_insert_slot(self, row, above):
        name = self.entry(row).name
        leader = self.group_leader(name)
        names = ({leader, *self._mod_groups[leader]["members"]}
                 if leader else {name})
        slots = [i for i, e in enumerate(self._natural) if e.name in names]
        before = above != self.reverse_mode_active
        return min(slots) if before else max(slots) + 1

    def sort_group_selection(self, rows):
        names = grouping.expand_leaders(
            [self.entry(r).name for r in rows], self._mod_groups)
        selected = [e for e in self._natural if e.name in names]
        if any(e.locked for e in selected):
            return False

        def ordered(items, key, conflicted):
            safe = sorted([item for item in items if not conflicted(item)], key=key)
            conflicts = [item for item in items if conflicted(item)]
            if self.reverse_mode_active:
                safe.reverse()
            return safe + conflicts

        result = []
        for block in grouping.blocks(self._natural, self._mod_groups):
            picked = [e for e in block if e.name in names]
            if picked and len(picked) != len(block):
                picked = ordered(picked, lambda e: e.name.casefold(),
                                 lambda e: bool(self.loose_conflict_code(e.name)))
                it = iter(picked)
                block = [next(it) if e.name in names else e for e in block]
            result.append(block)
        chosen = [b for b in result if all(e.name in names for e in b)]
        chosen = ordered(chosen,
                         lambda b: (self.group_leader(b[0].name) or b[0].name).casefold(),
                         lambda b: any(self.loose_conflict_code(e.name) for e in b))
        it = iter(chosen)
        arranged = [next(it) if all(e.name in names for e in b) else b for b in result]
        return self._commit_group_edit([e for b in arranged for e in b], self._mod_groups)
