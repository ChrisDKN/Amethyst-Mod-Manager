"""Modlist model - QAbstractTableModel over the ModEntry list.

Columns: Mod Name, Category, Flags, Conflicts, Installed, Version, Author,
Priority, Size (the checkbox is painted into column 0 by the delegate). Fed by
read_modlist; version / installed / flags / conflicts / authors are optional
dicts keyed by mod name (blank when absent). Index 0 = highest priority; the Priority column shows a descending
number (highest-priority row = largest value).
"""

from __future__ import annotations

from PySide6.QtCore import (
    Qt, QAbstractTableModel, QModelIndex, QMimeData, QByteArray, Signal,
    QT_TRANSLATE_NOOP,
)

# Crash-proof diagnostic prints (Flatpak stdout can raise BrokenPipeError and
# kill worker threads). See Utils.app_log.safe_print.
from Utils.app_log import safe_print as print  # noqa: A004
from Utils.modlist import ModEntry, read_modlist
from Utils.filemap import OVERWRITE_NAME, ROOT_FOLDER_NAME
from gui_qt.modlist_sort import (
    DIVIDER_NAME, build_display, uninvert_display, make_divider, is_reverse,
)

# UI-only boundary separators: pinned + locked, never written to modlist.txt.
# Overwrite floats at the top, Root Folder at the bottom (Tk parity).
_BOUNDARY_NAMES = (OVERWRITE_NAME, ROOT_FOLDER_NAME)
# All UI-only pinned rows: boundaries + the reverse-mode float divider.
_PINNED_NAMES = _BOUNDARY_NAMES + (DIVIDER_NAME,)


# Column indices. Order mirrors the Tk app: Category right after Name, Size last.
COL_NAME = 0
COL_CATEGORY = 1
COL_FLAGS = 2
COL_CONFLICTS = 3
COL_INSTALLED = 4
COL_VERSION = 5
COL_AUTHOR = 6
COL_PRIORITY = 7
COL_SIZE = 8
COLUMNS = ["Mod Name", "Category", "Flags", "Conflicts", "Installed",
           "Version", "Author", "Priority", "Size"]

# COLUMNS doubles as canonical persistence keys, so it must stay untranslated;
# headerData() translates each label at display time via self.tr(COLUMNS[i]).
# lupdate can't see through that dynamic tr(), so register the literals here
# under the ModListModel context (QT_TRANSLATE_NOOP marks-for-extraction only,
# returns the string unchanged).
_COLUMN_TR_MARKERS = [
    QT_TRANSLATE_NOOP("ModListModel", "Mod Name"),
    QT_TRANSLATE_NOOP("ModListModel", "Category"),
    QT_TRANSLATE_NOOP("ModListModel", "Flags"),
    QT_TRANSLATE_NOOP("ModListModel", "Conflicts"),
    QT_TRANSLATE_NOOP("ModListModel", "Installed"),
    QT_TRANSLATE_NOOP("ModListModel", "Version"),
    QT_TRANSLATE_NOOP("ModListModel", "Author"),
    QT_TRANSLATE_NOOP("ModListModel", "Priority"),
    QT_TRANSLATE_NOOP("ModListModel", "Size"),
]

# Custom roles for the delegate.
EntryRole = Qt.UserRole + 1        # the ModEntry
ConflictRole = Qt.UserRole + 2     # int: 0 none, 1 wins, -1 loses, 2 mixed (loose)
PriorityRole = Qt.UserRole + 3     # int display priority
FlagsRole = Qt.UserRole + 4        # int bitmask (gui_qt.modlist_data.FLAG_*)
BsaConflictRole = Qt.UserRole + 5  # int: BSA/BA2 archive conflict code
HighlightRole = Qt.UserRole + 6    # int: 0 none, 1 higher(green), -1 lower(red),
                                   #      2 anchor(orange, plugin-selected mod),
                                   #      3 requires(purple), -3 required-by(blue)
UuidConflictRole = Qt.UserRole + 7  # int: BG3 pak module-UUID conflict code

_MIME = "application/x-amethyst-modrows"


class ModListModel(QAbstractTableModel):
    # Mods were enabled/disabled and modlist.txt saved. Payload is
    # list[(mod_name, now_enabled)] - the window syncs plugins.txt to it
    # (Tk parity: _sync_plugins_for_toggle).
    enabled_changed = Signal(object)
    # modlist.txt write failed - the window surfaces a toast (console print
    # alone loses the user's reorder/toggle silently).
    save_failed = Signal(str)

    def __init__(self, entries: list[ModEntry] | None = None,
                 versions: dict[str, str] | None = None,
                 installed: dict[str, str] | None = None,
                 conflicts: dict[str, int] | None = None):
        super().__init__()
        # Source of truth: entries in natural modlist.txt order (boundaries
        # included). _entries is the DISPLAY list - when no column sort is
        # active it IS _natural (same object); a sort derives a permutation
        # (plus the divider row in reverse-priority mode). Both hold the same
        # ModEntry objects, so per-entry edits need no translation; save()
        # always writes from _natural.
        self._natural: list[ModEntry] = entries or []
        self._entries: list[ModEntry] = self._natural
        # Mod names as of the last load from disk. save() needs it to tell a
        # user removal (was in baseline, now gone → drop) from an entry an
        # install worker added after we loaded (in neither → keep).
        self._baseline_names: set[str] = {
            e.name for e in self._natural if not e.is_separator}
        # Active column sort ("name"/"category"/…/"priority") + direction.
        self._sort_key: str | None = None
        self._sort_ascending: bool = True
        # True while the "hide separators" filter is active - makes a column
        # sort flatten all mods into one list instead of sorting within each
        # separator group (which would leave mods clustered under the hidden
        # separators). Set by the app when the filter state changes.
        self._separators_hidden: bool = False
        # Reverse-mode divider entry, reused across rebuilds so an unchanged
        # layout compares identical (no spurious layoutChanged).
        self._divider: ModEntry = make_divider()
        self._versions = versions or {}
        self._installed = installed or {}
        self._categories: dict[str, str] = {}
        # Nexus uploader per mod (meta.ini uploadedBy - Author column).
        self._authors: dict[str, str] = {}
        # Nexus summary per mod - backs the name-column hover tooltip only
        # (no column of its own). Populated with the other meta on reload.
        self._descriptions: dict[str, str] = {}
        # Formatted mod folder sizes ("12 MB"). Computed lazily - only when the
        # Size column is visible - so a default-hidden Size costs no disk walk.
        self._sizes: dict[str, str] = {}
        # Raw byte counts backing the Size column sort.
        self._size_bytes: dict[str, int] = {}
        self._conflicts = conflicts or {}
        self._bsa_conflicts: dict[str, int] = {}
        self._uuid_conflicts: dict[str, int] = {}
        self._flags: dict[str, int] = {}
        # Per-mod user note text (for the Note flag's hover tooltip). Kept in sync
        # with the FLAG_NOTE bit; empty when no note.
        self._notes: dict[str, str] = {}
        # Mods modified in the Mod Files tab (excluded files / strip prefixes).
        # Kept separate from the meta-derived flags so a meta refresh doesn't
        # drop it; OR'd into the FlagsRole bitmask. See modlist_data.FLAG_MODIFIED_MF.
        self._modified_mf: set[str] = set()
        # Filemap-derived flag overlays (computed during the conflict/filemap
        # rebuild, not from meta.ini): mods with pre-RTX (natives/x64) files and
        # mods that own files with a custom root-routing rule. OR'd into FlagsRole.
        self._prertx_mods: set[str] = set()
        self._root_rule_mods: set[str] = set()
        # Live overlay: mods whose FOMOD recorded a fileDependency plugin that is
        # now present + enabled in the load order (→ rerun the FOMOD), mapped to
        # ("pending"|"active", [plugin names]) for the tooltip. Computed on every
        # plugin reload from meta.ini fomodPendingDeps. See FLAG_RERUN_FOMOD.
        self._rerun_fomod_mods: dict[str, tuple[str, list[str]]] = {}
        # Highlight state: mod names tinted green (wins over selection) / red
        # (loses to selection), and a set of "anchor" mods (orange) - the mod a
        # selected plugin belongs to. Driven by the view's cross-panel wiring.
        self._hl_higher: set[str] = set()
        self._hl_lower: set[str] = set()
        self._hl_anchor: set[str] = set()
        # Requirement highlights (View Requirements tab): mods the selection
        # requires (purple) / mods that require the selection (blue). Mutually
        # exclusive with the conflict sets - every set_highlights call replaces
        # all five, so whichever mode is active empties the other.
        self._hl_requires: set[str] = set()
        self._hl_required_by: set[str] = set()
        # When set, toggle/reorder edits are written back here.
        self.modlist_path = None
        # Optional callback invoked after a save (e.g. to rebuild conflicts).
        self.on_saved = None
        # Separator state (keyed by separator display name), persisted per
        # profile. collapsed → its mods are hidden; locked → its block can't be
        # dragged. Set via set_separator_state(); persistence lives in the view.
        self._collapsed: set[str] = set()
        self._sep_locks: dict[str, bool] = {}
        # Custom separator background colours, keyed by the internal
        # `..._separator` name (matches the Tk / profile_state storage key).
        self._sep_colors: dict[str, str] = {}
        # Custom deployment overrides ({"path","raw","mode","merge"}), keyed by
        # the internal name - drives the path badge painted on the separator.
        self._sep_deploy: dict[str, dict] = {}
        # Per-row memo for _separator_highlight (block walk is O(block size)
        # and data() asks per paint). Cleared on highlight/collapse/entry edits.
        self._sep_hl_cache: dict[int, int] = {}
        # File counts for the pinned Overwrite / Root Folder boundary rows -
        # they own a folder rather than a block of mods, so the "(N)" they show
        # counts files on disk. Filled by an async walk (see the app's
        # _refresh_boundary_counts); empty until it lands.
        self._boundary_counts: dict[str, int] = {}

    # ---- loading ----------------------------------------------------------
    @classmethod
    def from_modlist(cls, modlist_path, **kw) -> "ModListModel":
        return cls(read_modlist(modlist_path), **kw)

    @staticmethod
    def _with_boundaries(entries: list[ModEntry]) -> list[ModEntry]:
        """Wrap raw entries with the pinned Overwrite (top) + Root Folder
        (bottom) boundary separators. They're locked + UI-only."""
        body = [e for e in entries if e.name not in _PINNED_NAMES]
        top = ModEntry(OVERWRITE_NAME, True, True, True)
        bot = ModEntry(ROOT_FOLDER_NAME, True, True, True)
        return [top] + body + [bot]

    def set_entries(self, entries: list[ModEntry]) -> None:
        self.beginResetModel()
        self._natural = self._with_boundaries(entries)
        self._entries = self._derive_display()
        self._sep_hl_cache.clear()
        self._baseline_names = {
            e.name for e in entries if not e.is_separator}
        self.endResetModel()

    # ---- column sorting -----------------------------------------------------
    def set_sort(self, key: str | None, ascending: bool = True) -> None:
        """Set (or clear with None) the active column sort and rebuild the
        display order. The natural order is untouched."""
        key = key or None
        ascending = bool(ascending)
        if (key, ascending) == (self._sort_key, self._sort_ascending):
            return
        self._sort_key = key
        self._sort_ascending = ascending
        self._rebuild_display()

    def sort_state(self) -> tuple[str | None, bool]:
        return self._sort_key, self._sort_ascending

    def set_separators_hidden(self, hidden: bool) -> None:
        """Tell the model whether the 'hide separators' filter is active. When
        it flips while a (non-priority) column sort is live, the display is
        re-derived so mods sort as one flat list instead of within groups."""
        hidden = bool(hidden)
        if hidden == self._separators_hidden:
            return
        self._separators_hidden = hidden
        if self._sort_key and self._sort_key != "priority":
            self._rebuild_display()

    @property
    def reverse_mode_active(self) -> bool:
        """True in reverse-priority mode (priority ascending, 0 at top)."""
        return is_reverse(self._sort_key, self._sort_ascending)

    def natural_entries(self) -> list[ModEntry]:
        """Entries in natural modlist.txt order (boundaries included). Any
        code that rebuilds/persists the body MUST start from this, never from
        the display order."""
        return self._natural

    def _sort_ctx(self) -> dict:
        """Per-name data dicts for the sort key functions. Flags are the
        effective bits (meta flags + the filemap/Mod-Files overlays)."""
        flags: dict[str, int] = {}
        for e in self._natural:
            if not e.is_separator:
                flags[e.name] = self._effective_flags(e.name)
        return {
            "categories": self._categories,
            "versions": self._versions,
            "installed": self._installed,
            "authors": self._authors,
            "size_bytes": self._size_bytes,
            "flags": flags,
            "conflicts": self._conflicts,
        }

    def _derive_display(self) -> list[ModEntry]:
        if not self._sort_key:
            return self._natural
        return build_display(self._natural, self._sort_key,
                             self._sort_ascending, self._sort_ctx(),
                             divider=self._divider,
                             flatten_groups=self._separators_hidden)

    def _rebuild_display(self) -> None:
        """Re-derive the display list from the natural order + active sort.
        Uses layoutChanged with a persistent-index remap (by entry identity)
        so selection/scroll follow the rows. No-op if the order is unchanged."""
        old = self._entries
        new = self._derive_display()
        if len(new) == len(old) and all(a is b for a, b in zip(new, old)):
            self._entries = new
            return
        self.layoutAboutToBeChanged.emit()
        old_persist = self.persistentIndexList()
        pos_by_id = {id(e): i for i, e in enumerate(new)}
        self._entries = new
        new_persist = []
        for idx in old_persist:
            e = old[idx.row()] if 0 <= idx.row() < len(old) else None
            r = pos_by_id.get(id(e), -1) if e is not None else -1
            new_persist.append(self.index(r, idx.column()) if r >= 0
                               else QModelIndex())
        self.changePersistentIndexList(old_persist, new_persist)
        self._sep_hl_cache.clear()
        self.layoutChanged.emit()

    def _resort_if_key(self, *keys: str) -> None:
        """Rebuild the display when the active sort depends on data that just
        changed (avoids rebuild storms from unrelated async setters)."""
        if self._sort_key in keys:
            self._rebuild_display()

    def set_meta(self, versions: dict[str, str], installed: dict[str, str],
                 categories: dict[str, str],
                 descriptions: "dict[str, str] | None" = None,
                 authors: "dict[str, str] | None" = None) -> None:
        """Set the meta.ini-derived per-mod dicts (Version / Installed /
        Category / Author columns), repaint those columns, and re-sort if the
        active sort reads them. The reload pushes entries first and applies the
        meta async (reading one ini per mod is disk work).

        *descriptions* backs the name-column hover tooltip (no column repaint)."""
        self._versions = versions or {}
        self._installed = installed or {}
        self._categories = categories or {}
        self._descriptions = descriptions or {}
        self._authors = authors or {}
        if self._entries:
            self.dataChanged.emit(
                self.index(0, COL_CATEGORY),
                self.index(len(self._entries) - 1, COL_AUTHOR),
                [Qt.DisplayRole])
        self._resort_if_key("version", "installed", "category", "author")

    def set_sizes(self, sizes: dict[str, str],
                  size_bytes: dict[str, int] | None = None) -> None:
        """Set formatted mod sizes (Size column) + raw bytes (Size sort).
        Repaints just that column - used when the user enables Size from the
        column menu after first load."""
        self._sizes = sizes or {}
        if size_bytes is not None:
            self._size_bytes = size_bytes or {}
        if self._entries:
            self.dataChanged.emit(self.index(0, COL_SIZE),
                                  self.index(len(self._entries) - 1, COL_SIZE),
                                  [Qt.DisplayRole])
        self._resort_if_key("size")

    def set_flags(self, flags: dict[str, int]) -> None:
        self._flags = flags or {}
        if self._entries:
            self.dataChanged.emit(self.index(0, COL_FLAGS),
                                  self.index(len(self._entries) - 1, COL_FLAGS),
                                  [FlagsRole, Qt.DisplayRole])
        self._resort_if_key("flags")

    def set_notes(self, notes: dict[str, str]) -> None:
        """Store per-mod note text (for the Note flag's hover tooltip)."""
        self._notes = notes or {}

    def note_for(self, name: str) -> str:
        return self._notes.get(name, "")

    def set_modified_mf(self, mods: set[str]) -> None:
        """Set which mods are modified in the Mod Files tab (overlays the
        FLAG_MODIFIED_MF eye icon in the Flags column)."""
        self._modified_mf = set(mods or ())
        self._emit_flags_changed()

    def set_prertx_mods(self, mods: set[str]) -> None:
        """Set which mods contain pre-RTX (natives/x64) files - the info icon
        (filemap-derived overlay)."""
        self._prertx_mods = set(mods or ())
        self._emit_flags_changed()

    def set_root_rule_mods(self, mods: set[str]) -> None:
        """Set which mods own files with a custom root-routing rule - the root
        icon (filemap-derived overlay)."""
        self._root_rule_mods = set(mods or ())
        self._emit_flags_changed()

    def set_rerun_fomod_mods(self, mods) -> None:
        """Set which mods have a recorded FOMOD fileDependency plugin now present
        in the load order - the rerun-FOMOD icon (live overlay, recomputed on
        each plugin reload). *mods* maps mod name → ``("pending"|"active",
        [plugin names])`` for the hover tooltip (a bare iterable of names is
        accepted with empty reasons). See FLAG_RERUN_FOMOD."""
        if isinstance(mods, dict):
            self._rerun_fomod_mods = dict(mods)
        else:
            self._rerun_fomod_mods = {n: ("", []) for n in (mods or ())}
        self._emit_flags_changed()

    def rerun_fomod_reason(self, name: str):
        """``("pending"|"active", [plugin names])`` behind *name*'s rerun-FOMOD
        flag, or None - read by the delegate's flag tooltip."""
        return self._rerun_fomod_mods.get(name)

    def _emit_flags_changed(self) -> None:
        if self._entries:
            self.dataChanged.emit(self.index(0, COL_FLAGS),
                                  self.index(len(self._entries) - 1, COL_FLAGS),
                                  [FlagsRole, Qt.DisplayRole])
        self._resort_if_key("flags")

    def set_filemap_results(self, conflicts: dict[str, int],
                            bsa_conflicts: dict[str, int],
                            prertx: set[str], root_rule: set[str],
                            uuid_conflicts: "dict[str, int] | None" = None) -> None:
        """Apply everything a filemap rebuild produces for this model in ONE
        dataChanged pass. Equivalent to set_conflicts + set_prertx_mods +
        set_root_rule_mods, but those each emit a full-table dataChanged -
        three repaint/relayout storms per rebuild on a big modlist."""
        self._conflicts = conflicts or {}
        self._bsa_conflicts = bsa_conflicts or {}
        self._uuid_conflicts = uuid_conflicts or {}
        self._prertx_mods = set(prertx or ())
        self._root_rule_mods = set(root_rule or ())
        if self._entries:
            # COL_NAME..COL_CONFLICTS spans the Flags column too.
            self.dataChanged.emit(
                self.index(0, COL_NAME),
                self.index(len(self._entries) - 1, COL_CONFLICTS),
                [ConflictRole, BsaConflictRole, UuidConflictRole, FlagsRole,
                 Qt.DisplayRole])
        self._resort_if_key("conflicts", "flags")

    def set_conflicts(self, conflicts: dict[str, int],
                      bsa_conflicts: dict[str, int] | None = None,
                      uuid_conflicts: "dict[str, int] | None" = None) -> None:
        self._conflicts = conflicts or {}
        if bsa_conflicts is not None:
            self._bsa_conflicts = bsa_conflicts or {}
        if uuid_conflicts is not None:
            self._uuid_conflicts = uuid_conflicts or {}
        if self._entries:
            self.dataChanged.emit(self.index(0, COL_NAME),
                                  self.index(len(self._entries) - 1, COL_CONFLICTS),
                                  [ConflictRole, BsaConflictRole,
                                   UuidConflictRole, Qt.DisplayRole])
        self._resort_if_key("conflicts")

    def set_bsa_conflicts(self, bsa_conflicts: dict[str, int]) -> None:
        """Update ONLY the BSA conflict codes, leaving loose conflicts + flags
        untouched. Used when a plugin toggle/reorder changes BSA load order -
        the filemap/loose conflicts are unaffected, so only the BSA icons need
        repainting (Tk parity: recompute_bsa_conflicts)."""
        self._bsa_conflicts = bsa_conflicts or {}
        if self._entries:
            self.dataChanged.emit(self.index(0, COL_NAME),
                                  self.index(len(self._entries) - 1, COL_CONFLICTS),
                                  [BsaConflictRole, Qt.DisplayRole])
        self._resort_if_key("conflicts")

    def loose_conflict_code(self, name: str) -> int:
        """Loose-file conflict code for *name* (0 when the mod has no loose
        conflict; BSA-only conflicts don't count)."""
        return self._conflicts.get(name, 0)

    def _separator_highlight(self, row: int, e) -> int:
        """A separator is tinted ONLY when collapsed AND one of its child mods is
        a highlight partner (Tk parity - an expanded block tints the child mod
        directly instead). anchor(2) > higher(1) > lower(-1).
        Cached per row - data() asks for this on every paint, and the block walk
        is O(block size). Invalidated on highlight/collapse/entry changes."""
        cached = self._sep_hl_cache.get(row)
        if cached is not None:
            return cached
        code = self._separator_highlight_compute(row, e)
        self._sep_hl_cache[row] = code
        return code

    def _separator_highlight_compute(self, row: int, e) -> int:
        # [Overwrite] is a boundary separator but DOES light up (green) when it
        # wins over the selection - same as Tk. Root Folder never highlights.
        if e.name == OVERWRITE_NAME:
            if e.name in self._hl_anchor:
                return 2
            if e.name in self._hl_higher:
                return 1
            if e.name in self._hl_lower:
                return -1
            return 0
        if e.name in _BOUNDARY_NAMES:
            return 0
        if e.display_name not in self._collapsed:
            return 0
        anchor = requires = required_by = higher = lower = False
        for r in self.sep_block_rows(row):
            name = self._entries[r].name
            if name in self._hl_anchor:
                anchor = True
            elif name in self._hl_requires:
                requires = True
            elif name in self._hl_required_by:
                required_by = True
            elif name in self._hl_higher:
                higher = True
            elif name in self._hl_lower:
                lower = True
        if anchor:
            return 2
        if requires:
            return 3
        if required_by:
            return -3
        if higher:
            return 1
        if lower:
            return -1
        return 0

    def set_highlights(self, higher=None, lower=None, anchor=None,
                       requires=None, required_by=None) -> None:
        """Update conflict/anchor/requirement highlight sets and repaint the
        affected rows.

        Diff-aware: a no-op refresh (e.g. the per-rebuild self-highlight
        reapply with unchanged partners) emits nothing, and a real change
        repaints only the span of rows whose tint flipped - a full-table
        HighlightRole dataChanged costs ~50-150 ms on a 550-row modlist and
        this runs after every conflict rebuild."""
        higher = set(higher or ())
        lower = set(lower or ())
        anchor = set(anchor or ())
        requires = set(requires or ())
        required_by = set(required_by or ())
        if (higher == self._hl_higher and lower == self._hl_lower
                and anchor == self._hl_anchor
                and requires == self._hl_requires
                and required_by == self._hl_required_by):
            return
        changed = ((self._hl_higher ^ higher) | (self._hl_lower ^ lower)
                   | (self._hl_anchor ^ anchor)
                   | (self._hl_requires ^ requires)
                   | (self._hl_required_by ^ required_by))
        self._hl_higher = higher
        self._hl_lower = lower
        self._hl_anchor = anchor
        self._hl_requires = requires
        self._hl_required_by = required_by
        self._sep_hl_cache.clear()
        if not self._entries:
            return
        # Rows to repaint: mods whose membership flipped + collapsed separators
        # (their tint summarises hidden children; expanded ones never tint -
        # except Overwrite, which is covered by the name check).
        rows = [i for i, e in enumerate(self._entries)
                if e.name in changed
                or (e.is_separator and e.display_name in self._collapsed)]
        if not rows:
            return
        self.dataChanged.emit(self.index(min(rows), 0),
                              self.index(max(rows), COL_PRIORITY),
                              [HighlightRole])

    # ---- Qt model interface ----------------------------------------------
    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self._entries)

    def columnCount(self, parent=QModelIndex()):
        return len(COLUMNS)

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if orientation == Qt.Horizontal and role == Qt.DisplayRole:
            # TkStyleHeader paints the label itself (elided clear of the sort
            # triangle) - it suppresses the native text for the chrome pass.
            if getattr(self, "_suppress_header_text", False):
                return ""
            # The Name column hosts the column-menu button at its left edge;
            # pad the (left-aligned) label so it isn't drawn under the button.
            # Display-only + TRANSLATED - persistence still keys off the
            # canonical (untranslated) COLUMNS names elsewhere.
            label = self.tr(COLUMNS[section])
            if section == COL_NAME:
                return "    " + label
            return label
        return None

    def _priority_for_row(self, row: int) -> int:
        """Priority number for a display row. Normally the natural descending
        number (top of natural order = largest); in reverse-priority mode the
        display is the exact inversion, so counting non-separators ABOVE the
        display row yields the same per-mod value with 0 at the top."""
        e = self._entries[row]
        if e.is_separator:
            return -1
        if self.reverse_mode_active:
            return sum(1 for x in self._entries[:row] if not x.is_separator)
        if self._entries is self._natural:
            below = sum(1 for x in self._entries[row:] if not x.is_separator)
            return below - 1
        # Non-priority sort: the number reflects the NATURAL position (Tk
        # parity - sorting by name doesn't renumber priorities).
        try:
            ni = next(i for i, x in enumerate(self._natural) if x is e)
        except StopIteration:
            return -1
        below = sum(1 for x in self._natural[ni:] if not x.is_separator)
        return below - 1

    def _effective_flags(self, name: str) -> int:
        """Meta flag bits + the Mod-Files / filemap-derived overlays."""
        from gui_qt.modlist_data import (
            FLAG_MODIFIED_MF, FLAG_PRERTX, FLAG_ROOT_RULE, FLAG_RERUN_FOMOD)
        bits = self._flags.get(name, 0)
        if name in self._modified_mf:
            bits |= FLAG_MODIFIED_MF
        if name in self._prertx_mods:
            bits |= FLAG_PRERTX
        if name in self._root_rule_mods:
            bits |= FLAG_ROOT_RULE
        if name in self._rerun_fomod_mods:
            bits |= FLAG_RERUN_FOMOD
        return bits

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid():
            return None
        e = self._entries[index.row()]
        col = index.column()

        if role == EntryRole:
            return e
        if role == ConflictRole:
            return 0 if e.is_separator else self._conflicts.get(e.name, 0)
        if role == BsaConflictRole:
            return 0 if e.is_separator else self._bsa_conflicts.get(e.name, 0)
        if role == UuidConflictRole:
            return 0 if e.is_separator else self._uuid_conflicts.get(e.name, 0)
        if role == HighlightRole:
            if e.is_separator:
                return self._separator_highlight(index.row(), e)
            if e.name in self._hl_anchor:
                return 2
            if e.name in self._hl_requires:
                return 3
            if e.name in self._hl_required_by:
                return -3
            if e.name in self._hl_higher:
                return 1
            if e.name in self._hl_lower:
                return -1
            return 0
        if role == FlagsRole:
            return 0 if e.is_separator else self._effective_flags(e.name)
        if role == PriorityRole:
            return self._priority_for_row(index.row())

        if role == Qt.DisplayRole:
            if e.is_separator:
                return e.display_name if col == COL_NAME else ""
            if col == COL_NAME:
                return e.display_name
            if col == COL_CATEGORY:
                return self._categories.get(e.name, "")
            if col == COL_VERSION:
                return self._versions.get(e.name, "")
            if col == COL_INSTALLED:
                return self._installed.get(e.name, "")
            if col == COL_AUTHOR:
                return self._authors.get(e.name, "")
            if col == COL_SIZE:
                return self._sizes.get(e.name, "")
            if col == COL_PRIORITY:
                p = self._priority_for_row(index.row())
                return str(p) if p >= 0 else ""
            return ""
        return None

    def flags(self, index):
        if not index.isValid():
            return Qt.ItemIsDropEnabled
        e = self._entries[index.row()]
        # The reverse-mode float divider is a pure visual marker: enabled only
        # (not selectable, not draggable, not a drop target).
        if e.name == DIVIDER_NAME:
            return Qt.ItemIsEnabled
        f = Qt.ItemIsEnabled | Qt.ItemIsSelectable
        # Draggable unless pinned: boundary separators + locked MODS can't be
        # dragged (a regular separator reads as locked=True but IS draggable).
        pinned = e.name in _BOUNDARY_NAMES or (not e.is_separator and e.locked)
        if not pinned:
            f |= Qt.ItemIsDragEnabled
        if not e.is_separator:
            f |= Qt.ItemIsDropEnabled
        return f

    # ---- drop-validity (keep boundary separators pinned) ------------------
    def _movable_span(self) -> tuple[int, int]:
        """[lo, hi) row range mods may live in: below a leading locked
        separator (Overwrite) and above a trailing locked one (Root Folder)."""
        lo, hi = 0, len(self._entries)
        if self._entries and self._entries[0].is_separator and self._entries[0].locked:
            lo = 1
        if (self._entries and self._entries[-1].is_separator
                and self._entries[-1].locked):
            hi = len(self._entries) - 1
        return lo, hi

    def movable_span(self) -> tuple[int, int]:
        """Public accessor for the [lo, hi) valid-drop range (see
        _movable_span). The view clamps its drop indicator to this so the
        blue line never renders at/below the Root Folder boundary."""
        return self._movable_span()

    def canDropMimeData(self, data, action, row, col, parent):
        if action != Qt.MoveAction or not data.hasFormat(_MIME):
            return False
        dest = row if row != -1 else (parent.row() if parent.isValid()
                                      else len(self._entries))
        lo, hi = self._movable_span()
        return lo <= dest <= hi

    # ---- toggling ---------------------------------------------------------
    def toggle(self, row: int) -> None:
        from Utils.perftrace import span
        with span("model.toggle"):
            e = self._entries[row]
            if e.is_separator or e.locked:
                return
            e.enabled = not e.enabled
            # Whole row: the enabled state dims the text in EVERY column, not
            # just the Name cell with the checkbox.
            self.dataChanged.emit(self.index(row, 0),
                                  self.index(row, len(COLUMNS) - 1),
                                  [EntryRole, Qt.DisplayRole])
            self.save(edit_ctx=("toggle", [(e.name, e.enabled)]))
            self.enabled_changed.emit([(e.name, e.enabled)])

    def set_rows_enabled(self, rows, enabled: bool) -> None:
        """Enable/disable the mods at *rows* (skips separators + locked), then
        save + emit enabled_changed ONCE for the whole batch."""
        changed: list[tuple[str, bool]] = []
        changed_rows: list[int] = []
        for r in rows:
            e = self._entries[r]
            if e.is_separator or e.locked or e.enabled == enabled:
                continue
            e.enabled = enabled
            changed.append((e.name, enabled))
            changed_rows.append(r)
        # One dataChanged per contiguous run, not per row - Enable/Disable-All
        # on a large modlist would otherwise trigger a repaint per mod.
        changed_rows.sort()
        run_start = prev = None
        for r in changed_rows + [None]:
            if prev is not None and (r is None or r != prev + 1):
                self.dataChanged.emit(self.index(run_start, 0),
                                      self.index(prev, len(COLUMNS) - 1),
                                      [EntryRole, Qt.DisplayRole])
                run_start = None
            if r is not None and run_start is None:
                run_start = r
            prev = r
        if changed:
            self.save(edit_ctx=("toggle", list(changed)))
            self.enabled_changed.emit(changed)

    def entry(self, row: int) -> ModEntry:
        return self._entries[row]

    def mod_names(self) -> list[str]:
        """All non-separator mod names in the natural (unfiltered) order."""
        return [e.name for e in self._natural if not e.is_separator]

    def enabled_mod_names(self) -> set[str]:
        """Names of non-separator mods that are currently ENABLED."""
        return {e.name for e in self._natural
                if not e.is_separator and e.enabled}

    def description(self, name: str) -> str:
        """Nexus summary for *name* (name-column hover tooltip), or "" if none."""
        return self._descriptions.get(name, "")

    # ---- separators -------------------------------------------------------
    def set_separator_state(self, collapsed: set[str], locks: dict[str, bool],
                            colors: dict[str, str] | None = None,
                            deploy_paths: dict[str, dict] | None = None):
        self._collapsed = set(collapsed or set())
        self._sep_locks = dict(locks or {})
        if colors is not None:
            self._sep_colors = dict(colors)
        if deploy_paths is not None:
            self._sep_deploy = dict(deploy_paths)
        self._sep_hl_cache.clear()

    def is_collapsed(self, sep_name: str) -> bool:
        return sep_name in self._collapsed

    def is_sep_locked(self, sep_name: str) -> bool:
        return bool(self._sep_locks.get(sep_name, False))

    def sep_color(self, sep_name: str) -> str | None:
        """Custom background colour ("#rrggbb") for a separator, keyed by its
        internal `..._separator` name, or None if it uses the theme default."""
        return self._sep_colors.get(sep_name)

    def set_sep_color(self, sep_name: str, color: str | None) -> None:
        """Set/clear a separator's custom colour (None clears it)."""
        if color:
            self._sep_colors[sep_name] = color
        else:
            self._sep_colors.pop(sep_name, None)

    def set_boundary_counts(self, counts: dict[str, int] | None) -> None:
        """Set the on-disk file counts for the Overwrite / Root Folder rows."""
        self._boundary_counts = dict(counts or {})

    def boundary_file_count(self, sep_name: str) -> int | None:
        """File count for a boundary separator's folder, or None if unknown."""
        return self._boundary_counts.get(sep_name)

    def sep_deploy_info(self, sep_name: str) -> dict:
        """Deployment override ({"path","raw","mode","merge"}) for a separator,
        keyed by its internal `..._separator` name, or {} if none."""
        return self._sep_deploy.get(sep_name) or {}

    def set_sep_deploy_info(self, sep_name: str, info: dict | None) -> None:
        """Set/clear a separator's deployment override (None/empty clears it)."""
        if info:
            self._sep_deploy[sep_name] = dict(info)
        else:
            self._sep_deploy.pop(sep_name, None)

    def toggle_collapse(self, row: int) -> set[str]:
        e = self._entries[row]
        if not e.is_separator or e.name == DIVIDER_NAME:
            return self._collapsed
        name = e.display_name
        if name in self._collapsed:
            self._collapsed.discard(name)
        else:
            self._collapsed.add(name)
        self._sep_hl_cache.clear()
        return self._collapsed

    def toggle_sep_lock(self, row: int) -> dict[str, bool]:
        e = self._entries[row]
        if e.is_separator and e.name not in _PINNED_NAMES:
            name = e.display_name
            self._sep_locks[name] = not self._sep_locks.get(name, False)
        return self._sep_locks

    def set_sep_lock_range(self, row_a: int, row_b: int, locked: bool) -> dict[str, bool]:
        """Set the lock state of every (lockable) separator between *row_a* and
        *row_b* inclusive - used by shift-click range selection on the lock
        boxes. Pinned/boundary separators in the range are skipped."""
        lo, hi = sorted((row_a, row_b))
        lo = max(0, lo)
        hi = min(len(self._entries) - 1, hi)
        for r in range(lo, hi + 1):
            e = self._entries[r]
            if e.is_separator and e.name not in _PINNED_NAMES:
                self._sep_locks[e.display_name] = locked
        return self._sep_locks

    # ---- bulk separator / mod operations (footer buttons) -----------------
    def collapsible_separator_names(self) -> list[str]:
        """Display names of separators that can be collapsed (excludes the
        Overwrite / Root Folder boundaries + the reverse-mode divider)."""
        return [e.display_name for e in self._entries
                if e.is_separator and e.name not in _PINNED_NAMES]

    def any_collapsed(self) -> bool:
        names = self.collapsible_separator_names()
        return any(n in self._collapsed for n in names)

    def set_all_collapsed(self, collapsed: bool) -> set[str]:
        """Collapse or expand every (non-boundary) separator. Returns the new
        collapsed set so the view can persist it."""
        names = set(self.collapsible_separator_names())
        if collapsed:
            self._collapsed |= names
        else:
            self._collapsed -= names
        return self._collapsed

    def all_mods_enabled(self) -> bool:
        mods = [e for e in self._entries if not e.is_separator and not e.locked]
        return bool(mods) and all(e.enabled for e in mods)

    def has_disabled_mods(self) -> bool:
        return any(not e.is_separator and not e.locked and not e.enabled
                   for e in self._entries)

    def set_all_enabled(self, enabled: bool) -> None:
        """Enable/disable every toggleable mod, then save once."""
        changed: list[tuple[str, bool]] = []
        for r, e in enumerate(self._entries):
            if e.is_separator or e.locked:
                continue
            if e.enabled != enabled:
                e.enabled = enabled
                changed.append((e.name, enabled))
        if changed:
            self.dataChanged.emit(
                self.index(0, 0),
                self.index(len(self._entries) - 1, len(COLUMNS) - 1),
                [EntryRole, Qt.DisplayRole])
            self.save(edit_ctx=("toggle", list(changed)))
            self.enabled_changed.emit(changed)

    def hidden_rows(self) -> set[int]:
        """Rows to hide: mods that fall under a collapsed separator (up to the
        next separator). Separators themselves are never hidden.

        The Overwrite / Root Folder boundaries never collapse their block - they
        aren't user-collapsible (Tk excludes them from the toggle set), so a
        stale '[Overwrite]' in the persisted collapsed set must NOT hide the
        ungrouped mods that sit between Overwrite and the first real separator.

        While the 'hide separators' filter is active the separators aren't shown
        at all, so their collapse state is meaningless - every mod stays visible
        (a collapsed separator must not swallow its mods behind a hidden header).
        """
        if self._separators_hidden:
            return set()
        hidden: set[int] = set()
        collapsing = False
        for i, e in enumerate(self._entries):
            if e.is_separator:
                collapsing = (e.name not in _BOUNDARY_NAMES
                              and e.display_name in self._collapsed)
            elif collapsing:
                hidden.add(i)
        return hidden

    def sep_block_rows(self, sep_row: int) -> range:
        """Row range [sep_row+1, next-separator) - the mods a separator owns."""
        end = sep_row + 1
        while end < len(self._entries) and not self._entries[end].is_separator:
            end += 1
        return range(sep_row + 1, end)

    def sep_block_priority_range(self, sep_row: int) -> str:
        """Priority range of the mods a separator owns, for the collapsed-
        separator Priority column (Tk parity - e.g. "0 - 20", or "5" when the
        block holds a single mod). Empty when the block has no mods."""
        prios = [
            self._priority_for_row(r)
            for r in self.sep_block_rows(sep_row)
            if not self._entries[r].is_separator
        ]
        prios = [p for p in prios if p >= 0]
        if not prios:
            return ""
        lo, hi = min(prios), max(prios)
        return str(lo) if lo == hi else f"{lo} - {hi}"

    def sep_block_summary(self, block) -> tuple[int, set, set, set]:
        """(flag bits, loose codes, BSA codes, UUID codes) for the
        mods at *block* display rows - the collapsed-separator icon summary.
        Walks the dicts directly: the delegate asks per paint, and two
        data()/QModelIndex round-trips per row add up on big blocks."""
        bits = 0
        codes: set = set()
        bsa: set = set()
        uuid: set = set()
        for r in block:
            e = self._entries[r]
            if e.is_separator:
                continue
            bits |= self._effective_flags(e.name)
            cc = self._conflicts.get(e.name, 0)
            if cc:
                codes.add(cc)
            bc = self._bsa_conflicts.get(e.name, 0)
            if bc:
                bsa.add(bc)
            uc = self._uuid_conflicts.get(e.name, 0)
            if uc:
                uuid.add(uc)
        return bits, codes, bsa, uuid

    # ---- persistence ------------------------------------------------------
    def save(self, edit_ctx=None) -> None:
        """Write the current entries back to modlist.txt (no-op if no path).
        The Overwrite / Root Folder boundary separators are UI-only and are
        stripped before writing. Fires on_saved(edit_ctx) so the view can
        rebuild. edit_ctx tells the app what kind of edit this save carries
        so it can take a cheaper path than the full conflict rebuild:
          ("move", moved_names, crossed_names) - pure reorder (drag /
            set_priority / move-to-separator / sort-selection)
          ("toggle", [(name, enabled), ...])   - enable/disable only
          None - anything else (or an unclassifiable edit) → full rebuild."""
        if self.modlist_path is None:
            return
        # Every structural edit (drag, remove, add-separator, set_priority…)
        # funnels through here - row→block mapping may have changed.
        self._sep_hl_cache.clear()
        from Utils.modlist import read_modlist, write_modlist, modlist_lock
        from Utils.perftrace import span
        # ALWAYS write the natural order - the display list may be a sorted /
        # inverted permutation (and contains the divider in reverse mode).
        body = [e for e in self._natural if e.name not in _PINNED_NAMES]
        try:
            # This model can be a stale snapshot - a background install writes
            # its new entry before our _reload_modlist lands - so writing body
            # verbatim would erase it. Take prepend_mod's lock and keep any
            # on-disk mod we've never seen; entries known at load but gone from
            # body were removed by the user and stay removed.
            with span("modlist.write_modlist"), modlist_lock(self.modlist_path):
                body_names = {e.name for e in body}
                known = body_names | self._baseline_names
                external = [e for e in read_modlist(self.modlist_path)
                            if not e.is_separator and e.name not in known]
                if external:
                    print(f"[gui_qt] modlist save: preserving "
                          f"{len(external)} externally added entr(y/ies): "
                          f"{', '.join(e.name for e in external[:5])}",
                          flush=True)
                write_modlist(self.modlist_path, external + body)
        except Exception as exc:
            print(f"[gui_qt] modlist save failed: {exc}", flush=True)
            self.save_failed.emit(f"Modlist save failed: {exc}")
            return
        if self.on_saved:
            with span("modlist.on_saved(kickoff)"):
                self.on_saved(edit_ctx)

    # ---- structural edits (context-menu actions) --------------------------
    def rename(self, row: int, new_name: str) -> None:
        e = self._entries[row]
        # Block only pinned boundaries + locked mods (separators read as locked
        # but are renamable).
        if e.name in _PINNED_NAMES or (not e.is_separator and e.locked):
            return
        # Separators keep their suffix so they stay separators on write-out.
        from Utils.modlist import _SEPARATOR_SUFFIX
        e.name = (new_name + _SEPARATOR_SUFFIX) if e.is_separator else new_name
        idx = self.index(row, COL_NAME)
        self.dataChanged.emit(idx, idx, [Qt.DisplayRole, EntryRole])
        self.save()

    def _natural_row_of(self, e: ModEntry) -> int:
        """Position of an entry object in the natural list (identity match)."""
        for i, x in enumerate(self._natural):
            if x is e:
                return i
        return -1

    def set_priority(self, row: int, priority: int) -> None:
        """Move a mod so its descending-priority number becomes *priority*.
        Re-positions within the NATURAL non-separator ordering (clamped)."""
        e = self._entries[row]
        if e.is_separator:
            return
        nat = self._natural
        nonsep = [i for i, x in enumerate(nat) if not x.is_separator]
        n = len(nonsep)
        target_from_top = max(0, min(n - 1, n - 1 - priority))
        dest_row = nonsep[target_from_top]
        if self._entries is nat:
            if dest_row != row:
                self.move_block([row],
                                dest_row if dest_row < row else dest_row + 1)
            return
        # Sorted display: splice the natural list, then re-derive.
        src = self._natural_row_of(e)
        if src < 0 or dest_row == src:
            return
        nat.pop(src)
        # Pre-removal target is dest_row (moving up: before it) or dest_row+1
        # (moving down: after it); the pop shifts the latter down by one, so
        # both cases land at dest_row post-pop.
        nat.insert(dest_row, e)
        self._rebuild_display()
        self.save()

    def add_separator(self, row: int, name: str, above: bool) -> None:
        from Utils.modlist import _SEPARATOR_SUFFIX
        from gui_qt.modlist_sort import insert_separator_display
        sep = ModEntry(name + _SEPARATOR_SUFFIX, True, False, True)
        ref = self._entries[row]
        if self.reverse_mode_active:
            # Inverted display reverses group/mod order, so a natural-order
            # insert mis-anchors - resolve in display space and uninvert
            # (Tk _add_separator_inverted).
            self._natural = insert_separator_display(self._entries, row,
                                                     above, sep)
            self._rebuild_display()
            self.save()
            return
        if self._entries is self._natural:
            at = row if above else row + 1
            self.beginInsertRows(QModelIndex(), at, at)
            self._entries.insert(at, sep)
            self.endInsertRows()
            self.save()
            return
        # Non-priority sort: insert next to the anchor entry in NATURAL order
        # (Tk operates on the natural list; the display re-sorts around it).
        ni = self._natural_row_of(ref)
        if ni < 0:
            return
        self._natural.insert(ni if above else ni + 1, sep)
        self._rebuild_display()
        self.save()

    def insert_mod(self, row: int, name: str, above: bool = False) -> None:
        """Insert an (enabled) mod entry named *name* relative to *row*. Used by
        'Create empty mod below' - the folder/meta.ini are created by the caller."""
        entry = ModEntry(name, True, False, False)
        if self._entries is self._natural:
            at = row if above else row + 1
            self.beginInsertRows(QModelIndex(), at, at)
            self._entries.insert(at, entry)
            self.endInsertRows()
            self.save()
            return
        ref = self._entries[row]
        ni = self._natural_row_of(ref)
        if ni < 0:
            return
        if self.reverse_mode_active:
            # Inverted display: visually below *ref* = BEFORE it in natural
            # order (and vice versa) - mirror add_separator's reverse branch.
            at = ni + 1 if above else ni
        else:
            at = ni if above else ni + 1
        self._natural.insert(at, entry)
        self._rebuild_display()
        self.save()

    def insert_mod_at_body_edge(self, top: bool, name: str) -> None:
        """Insert an (enabled) mod at the top or bottom of the natural body,
        just inside the boundary separators. Used by the boundary rows' 'Create
        an empty mod below' - normal mode drops it below Overwrite (top of the
        body), reverse-priority mode below Root Folder (bottom of the body)."""
        entry = ModEntry(name, True, False, False)
        # Natural layout is normally [Overwrite] + body + [Root Folder]; drop the
        # mod just inside whichever boundary the caller asked for. Anchor on the
        # boundary's real position (not a fixed 0/-1) so it's correct even if a
        # boundary is somehow absent.
        if top:
            at = next((i + 1 for i, e in enumerate(self._natural)
                       if e.name == OVERWRITE_NAME), 0)
        else:
            at = next((i for i, e in enumerate(self._natural)
                       if e.name == ROOT_FOLDER_NAME), len(self._natural))
        if self._entries is self._natural:
            self.beginInsertRows(QModelIndex(), at, at)
            self._natural.insert(at, entry)
            self.endInsertRows()
            self.save()
            return
        self._natural.insert(at, entry)
        self._rebuild_display()
        self.save()

    def remove_row(self, row: int, save: bool = True) -> None:
        """Drop *row*. Pass save=False when removing several rows in a loop and
        call save() once at the end - save() fires on_saved() which kicks off a
        full conflict/filemap rebuild, so per-row saving rebuilds N times."""
        e = self._entries[row]
        if e.name in _PINNED_NAMES or (not e.is_separator and e.locked):
            return
        self.beginRemoveRows(QModelIndex(), row, row)
        del self._entries[row]
        if self._entries is not self._natural:
            ni = self._natural_row_of(e)
            if ni >= 0:
                del self._natural[ni]
        self.endRemoveRows()
        if save:
            self.save()

    # ---- drag-reorder (beginMoveRows; selection/scroll preserved) ---------
    def supportedDropActions(self):
        return Qt.MoveAction

    def mimeTypes(self):
        return [_MIME]

    def mimeData(self, indexes):
        rows = sorted({i.row() for i in indexes})
        md = QMimeData()
        md.setData(_MIME, QByteArray(",".join(map(str, rows)).encode()))
        return md

    def dropMimeData(self, data, action, row, col, parent):
        if action != Qt.MoveAction or not data.hasFormat(_MIME):
            return False
        src = [int(x) for x in bytes(data.data(_MIME)).decode().split(",")]
        dest = row if row != -1 else (parent.row() if parent.isValid()
                                      else len(self._entries))
        # A separator drag (lone line, or a LOCKED separator carrying its block)
        # must land at a block boundary, never inside another separator's mods.
        dragging_sep = len(src) == 1 and self._entries[src[0]].is_separator
        if dragging_sep and self.is_sep_locked(self._entries[src[0]].display_name):
            src = [src[0]] + list(self.sep_block_rows(src[0]))
        dest = self._resolve_drop_dest(dest, separator_drag=dragging_sep,
                                       src_start=min(src))
        return self.move_block(src, dest)

    def _resolve_drop_dest(self, dest: int, separator_drag: bool,
                           src_start: int | None = None) -> int:
        """Tk drop rule: a separator (lone or carrying its block) lands at a
        block BOUNDARY so it stays self-contained and never splits another
        separator's mods. A plain mod drops as-is.

        Direction matters. Dragging DOWN, the separator lands at the END of the
        block it was dropped into (just before the next separator) so it doesn't
        split that block from the top. Dragging UP it lands EXACTLY at the drop
        point: the gap above the mod under the cursor is already a valid group
        start, so the separator sits there and absorbs that mod (and the rest of
        the block below it) - nothing above the drop point moves. Forward-snapping
        an upward drop is wrong: it would either run back into the source
        separator (a no-op) or swallow the whole preceding block."""
        n = len(self._entries)
        dest = max(0, min(dest, n))
        if not separator_drag:
            return dest
        # Upward drag: drop right where released; the gap is its own boundary.
        if src_start is not None and dest <= src_start:
            return dest
        i = dest
        while i < n and not self._entries[i].is_separator:
            i += 1
        return i

    def _mod_name_order(self) -> list[str]:
        """Mod names (separators excluded) in natural/priority order."""
        return [e.name for e in self._natural if not e.is_separator]

    @staticmethod
    def _move_ctx(old_order: list[str], new_order: list[str],
                  moved_names: list[str]):
        """Build the "move" edit_ctx payload: (moved_names, crossed_names).

        crossed = the union over moved mods of every OTHER mod whose priority
        order relative to that mod flipped. Handles non-contiguous moved sets
        (sort-selection) - non-moved pairs never flip, so only pairs involving
        a moved mod are tested. The union may over-report for multi-mod moves
        (a partner of one moved mod crossed only by another) - that costs an
        unnecessary rebuild, never a wrong skip. Returns None (= caller must
        rebuild) if a moved name is missing from either order - the edit
        wasn't the pure reorder this reasoning assumes - or if the pairwise
        sweep would be too big to run on the UI thread."""
        if not moved_names:
            return ([], [])
        old_idx = {n: i for i, n in enumerate(old_order)}
        new_idx = {n: i for i, n in enumerate(new_order)}
        if len(moved_names) * len(old_order) > 500_000:
            return None
        pairs = [(n, i, new_idx[n]) for n, i in old_idx.items()
                 if n in new_idx]
        crossed: set[str] = set()
        for m in moved_names:
            om, nm = old_idx.get(m), new_idx.get(m)
            if om is None or nm is None:
                return None
            for n, oi, ni in pairs:
                if n != m and (oi < om) != (ni < nm):
                    crossed.add(n)
        return (moved_names, sorted(crossed))

    def move_block(self, src_rows: list[int], dest: int) -> bool:
        """Move a contiguous block of rows to *dest* using beginMoveRows so the
        view animates and keeps selection/scroll (unlike a full reset).

        Natural-order only: while a column sort is active the display is a
        permutation and row moves are meaningless here - the drag path clears
        a non-priority sort first, and reverse-priority drags go through
        move_block_display()."""
        if not src_rows or self._entries is not self._natural:
            return False
        src_rows = sorted(src_rows)
        first, last = src_rows[0], src_rows[-1]
        # Pinned rows never move: boundary separators (Overwrite/Root Folder)
        # and locked MODS. NB a regular separator reads back as locked=True
        # (the '-' prefix convention in read_modlist) yet is still movable - so
        # the lock guard applies to mods only, plus the boundary names.
        for r in src_rows:
            e = self._entries[r]
            if e.name in _PINNED_NAMES:
                return False
            if not e.is_separator and e.locked:
                return False
        # Keep within the movable span (below Overwrite, above Root Folder).
        lo, hi = self._movable_span()
        if first < lo or last >= hi or not (lo <= dest <= hi):
            return False
        # Mods may be dragged freely into or out of a locked separator's block
        # (locking now only means the separator carries its block when the
        # SEPARATOR itself is dragged - it no longer traps loose mods).
        # Qt's beginMoveRows requires dest outside the moved range.
        if first <= dest <= last + 1:
            return False
        if not self.beginMoveRows(QModelIndex(), first, last,
                                  QModelIndex(), dest):
            return False
        from Utils.perftrace import span
        with span("model.move_block"):
            old_order = self._mod_name_order()
            block = self._entries[first:last + 1]
            del self._entries[first:last + 1]
            insert_at = dest if dest < first else dest - len(block)
            self._entries[insert_at:insert_at] = block
            self.endMoveRows()
            moved = [e.name for e in block if not e.is_separator]
            ctx = self._move_ctx(old_order, self._mod_name_order(), moved)
            self.save(edit_ctx=None if ctx is None else ("move",) + ctx)
        return True

    def move_block_display(self, src_rows: list[int], slot: int,
                           hidden: set[int] | frozenset = frozenset()) -> bool:
        """Reverse-priority drag commit: move rows [first..last] of the DISPLAY
        list to drop *slot*, applying the Tk reverse-mode drop semantics
        (join-group #165 guard, top clamp, divider slot, full-block exemption),
        then re-derive the natural order via uninvert (Tk
        _uninvert_entries_order) and save."""
        from gui_qt.modlist_sort import resolve_reverse_drop
        if not src_rows or not self.reverse_mode_active:
            return False
        src_rows = sorted(src_rows)
        first, last = src_rows[0], src_rows[-1]
        for r in src_rows:
            e = self._entries[r]
            if e.name in _PINNED_NAMES:
                return False
            if not e.is_separator and e.locked:
                return False
        lo, hi = self._movable_span()
        if first < lo or last >= hi:
            return False
        # Full separator block = the separator plus exactly its own mods
        # moving as a unit (exempt from the join-group branch + top clamp).
        full_block = (self._entries[first].is_separator
                      and last in self.sep_block_rows(first)
                      and all(not self._entries[r].is_separator
                              for r in range(first + 1, last + 1)))
        ins = resolve_reverse_drop(self._entries, slot, set(src_rows),
                                   full_block, hidden=hidden)
        # Mods drag freely into/out of a locked separator's block (locking only
        # keeps the separator's block together when the SEPARATOR is dragged).
        ins = max(lo, min(ins, hi + 1))
        if first <= ins <= last + 1:
            return False
        if not self.beginMoveRows(QModelIndex(), first, last,
                                  QModelIndex(), ins):
            return False
        old_order = self._mod_name_order()
        block = self._entries[first:last + 1]
        # The display list must become independent of _natural before the
        # splice (it IS a derived list in reverse mode, but guard anyway).
        if self._entries is self._natural:
            self._entries = list(self._natural)
        del self._entries[first:last + 1]
        at = ins if ins < first else ins - len(block)
        self._entries[at:at] = block
        self.endMoveRows()
        # Uninvert the mutated display into the new natural order, then
        # re-canonicalise the display (a drop below Overwrite belongs in the
        # float; everywhere else this is a no-op - invert∘uninvert identity).
        self._natural = uninvert_display(self._entries)
        self._sep_hl_cache.clear()
        self._rebuild_display()
        moved = [e.name for e in block if not e.is_separator]
        ctx = self._move_ctx(old_order, self._mod_name_order(), moved)
        self.save(edit_ctx=None if ctx is None else ("move",) + ctx)
        return True
