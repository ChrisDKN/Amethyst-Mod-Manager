"""Right-click context menu for the modlist.

Mirrors the Tk menu (gui/modlist_panel.py `_populate_context_menu`) for all three
target types - normal mods, separators, and the Overwrite folder. Each item is
SHOWN only when its Tk condition holds and HIDDEN otherwise (Tk omits items; it
never disables them). Any remaining greyed items are the handful still awaiting a
Qt backend, and even those appear only when their Tk show-condition passes.
"""

from __future__ import annotations

# Crash-proof diagnostic prints (Flatpak stdout can raise BrokenPipeError and
# kill worker threads). See Utils.app_log.safe_print.
from Utils.app_log import safe_print as print  # noqa: A004

from PySide6.QtWidgets import QMenu
from PySide6.QtGui import QAction
from PySide6.QtCore import QCoreApplication, QT_TRANSLATE_NOOP

from gui_qt.confirm_overlay import ConfirmOverlay
from gui_qt.i18n import profile_display
from gui_qt.modlist_model import COL_NAME
from gui_qt.text_input_overlay import TextInputOverlay


def _shortcut_hint(action_id: str) -> str:
    """Display-only hint for the matching configurable global shortcut."""
    try:
        from gui_qt.shortcuts import shortcut_text
        return shortcut_text(action_id)
    except Exception:
        return {"rename": "F2", "remove": "Del",
                "toggle_selected": "Enter"}.get(action_id, "")


def _mt(label: str) -> str:
    """Translate a modlist context-menu label. These live in module-level
    functions (no `self`), so translate via QCoreApplication under a shared
    "ModListMenu" context. The label literals are registered for lupdate in
    _TR_MARKERS at the bottom of this file (lupdate can't see through this
    helper, so that explicit list is the extraction source of truth)."""
    return QCoreApplication.translate("ModListMenu", label)


def _mtf(template: str, *args) -> str:
    """Like _mt but for count-labels: translate the {0}-template then format.
    e.g. _mtf("Remove mod ({0})", n)."""
    return QCoreApplication.translate("ModListMenu", template).format(*args)


def show_context_menu(view, global_pos, index):
    """Build + exec the context menu for *index* at *global_pos*."""
    menu = build_context_menu(view, index)
    if menu is not None:
        menu.exec(global_pos)


def build_context_menu(view, index):
    """Construct (but don't exec) the context QMenu for *index* - split out so
    headless tests can inspect the actions. Returns None if there's no menu."""
    model = view.model()
    if not index.isValid():
        return None
    row = index.row()
    entry = model.entry(row)
    # Fresh meta.ini memo per build - the gate helpers re-read the same metas
    # many times per selected mod (see _read_mod_meta).
    view._menu_meta_cache = {}

    # Selected rows (mods + separators tracked separately for the bulk actions).
    sel_rows = sorted({i.row() for i in view.selectionModel().selectedRows()
                       or view.selectionModel().selectedIndexes()})
    sel_mods = [r for r in sel_rows
                if not model.entry(r).is_separator]
    sel_seps = [r for r in sel_rows
                if model.entry(r).is_separator
                and model.entry(r).name not in _boundary_names()]
    multi_mods = len(sel_mods) > 1
    multi_seps = len(sel_seps) > 1

    menu = QMenu(view)
    # Track whether the current group emitted anything, so dividers only appear
    # between non-empty groups (Tk behaviour).
    state = {"group_started": False, "any": False}

    def _connect(action, slot):
        # QAction.triggered emits a `checked` bool. If a slot captures data via a
        # default arg (e.g. `lambda ns=names:`), Qt passes `checked` positionally
        # and clobbers that default. Wrap so the bool is always swallowed.
        action.triggered.connect(lambda _checked=False, _s=slot: _s())

    def act(label, slot, enabled=True, shortcut=None):
        # `label` is already translated by the caller (via _mt / _mtf); helpers
        # never translate, so count-templates like _mtf("… ({0})", n) work.
        a = QAction(label, menu)
        _connect(a, slot)
        a.setEnabled(enabled)
        if shortcut:
            # Display-only: show the matching global QShortcut (see shortcuts.py)
            # right-aligned in the menu. We set the shortcut TEXT via a tab in the
            # label rather than QAction.setShortcut() so no duplicate accelerator
            # is registered (which would trigger Qt's ambiguous-shortcut warning
            # and could steal the key from the real window-level QShortcut).
            a.setText(f"{label}\t{shortcut}")  # i18n: skip — label already tr'd by caller
        menu.addAction(a)
        state["group_started"] = True
        state["any"] = True
        return a

    def stub(label):
        # Greyed-out placeholder for an action not yet wired.
        return act(label, lambda: None, enabled=False)

    def submenu(label, items, enabled=True, scroll_cap=0):
        """Add a nested QMenu. *items* is a list of (text, slot) pairs - one
        action each. Used for Copy/Move to profile (the profile list nests as a
        submenu instead of opening a picker window).

        *scroll_cap* > 0 caps the visible height at that many rows: past the cap
        the submenu holds a single QWidgetAction wrapping a scrollable list
        instead of plain actions (used by Move to separator). QMenu's own
        scroller can't do this - it only engages when the popup exceeds the
        SCREEN height, and a max-height-constrained QMenu just clips and
        mis-positions."""
        # `label` is already translated by the caller.
        sub = QMenu(label, menu)
        sub.setEnabled(enabled)
        if scroll_cap and len(items) > scroll_cap:
            _fill_scroll_submenu(menu, sub, items, scroll_cap)
        else:
            for text, slot in items:
                # Profile names in items are DATA (not translated).
                a = QAction(text, sub)
                _connect(a, slot)
                sub.addAction(a)
        menu.addMenu(sub)
        state["group_started"] = True
        state["any"] = True
        return sub

    def divider():
        if state["group_started"]:
            menu.addSeparator()
            state["group_started"] = False

    if entry.is_separator and entry.name in _boundary_names():
        # The synthetic Overwrite / Root Folder rows share a small menu:
        #   Open folder - both (they resolve to a real on-disk folder)
        #   Log         - both (files swept in on restore; Root Folder gets its
        #                 own .mm_overwrite_log.txt written by _move_runtime_files)
        #   Show Conflicts - Overwrite only (Root Folder has no conflict data)
        from Utils.filegraph.constants import OVERWRITE_NAME, ROOT_FOLDER_NAME
        if multi_mods or multi_seps:
            return None
        has_game = getattr(view, "game", None) is not None
        _staging_ok = getattr(view, "staging_dir", None) is not None
        act(_mt("Open folder"), lambda: _open_folder(view, model, row))
        act(_mt("Log"), lambda: _show_overwrite_log(view, entry.name),
            enabled=has_game)
        if entry.name == OVERWRITE_NAME:
            act(_mt("Manage Overwrite…"), lambda: _manage_overwrite(view),
                enabled=has_game and _staging_ok)
        if entry.name == OVERWRITE_NAME and _has_conflict(model, row):
            act(_mt("Show Conflicts"), lambda: _show_conflicts(view, entry.name))
            act(_mt("Clear Conflict Filter")
                if _conflict_filter_on(view, entry.name) else _mt("Filter Conflicts"),
                lambda: _filter_conflicts(view, entry.name))
        elif _conflict_filter_on(view):
            act(_mt("Clear Conflict Filter"), lambda: _clear_conflict_filter(view))
        # Create an empty mod below - lives on the Overwrite row in normal mode
        # and the Root Folder row in reverse-priority mode, so it stays usable
        # even when the modlist has no mods to right-click.
        reverse = model.reverse_mode_active
        on_this_boundary = (entry.name == ROOT_FOLDER_NAME if reverse
                            else entry.name == OVERWRITE_NAME)
        if _staging_ok and on_this_boundary:
            act(_mt("Create an empty mod below"),
                lambda: _create_empty_mod_at_boundary(view, model, top=not reverse))
        return menu

    if entry.is_separator:
        _build_separator_menu(view, model, row, entry, sel_seps, multi_seps,
                              act, stub, divider)
    else:
        _build_mod_menu(view, model, row, entry, sel_mods, multi_mods,
                        act, stub, divider, submenu)
    return menu


def _build_separator_menu(view, model, row, entry, sel_seps, multi, act, stub, divider):
    if multi:
        # ≥2 separators selected.
        all_locked = all(model.is_sep_locked(model.entry(r).display_name)
                         for r in sel_seps)
        n = len(sel_seps)
        act(_mtf("{0} ({1})", _mt("Unlock Separators") if all_locked else _mt("Lock Separators"), n),
            lambda: _set_sep_locks_multi(view, model, sel_seps, not all_locked))
        divider()
        act(_mtf("Remove separators ({0})", n),
            lambda: _remove_separators_multi(view, model, sel_seps))
        return
    locked = model.is_sep_locked(entry.display_name)
    act(_mt("Unlock Separator") if locked else _mt("Lock Separator"),
        lambda: _toggle_sep_lock(view, model, row))
    divider()
    act(_mt("Rename separator"), lambda: _rename(view, model, row),
        shortcut=_shortcut_hint("rename"))
    act(_mt("Separator settings…"), lambda: _open_sep_settings(view, model, row))
    act(_mt("Add separator above"), lambda: _add_separator(view, model, row, True))
    act(_mt("Add separator below"), lambda: _add_separator(view, model, row, False))
    divider()
    act(_mt("Remove separator"), lambda: _remove_separator(view, model, row))


def _build_mod_menu(view, model, row, entry, sel_mods, multi, act, stub, divider,
                    submenu):
    if multi:
        n = len(sel_mods)
        _names = [model.entry(r).name for r in sel_mods]
        _staging_ok = getattr(view, "staging_dir", None) is not None
        # Group: files - Root Folder toggles gate on the non-empty subset each
        # applies to (Tk root_folder_enable_multi / _disable_multi).
        if _staging_ok:
            _rf_disable = [nm for nm in _names if _is_root_folder(view, nm)]
            _rf_enable = [nm for nm in _names if not _is_root_folder(view, nm)]
            if _rf_disable:
                act(_mtf("Disable Root Folder install ({0})", len(_rf_disable)),
                    lambda ns=_rf_disable: _toggle_root_folder(view, ns, False))
            if _rf_enable:
                act(_mtf("Enable Root Folder install ({0})", len(_rf_enable)),
                    lambda ns=_rf_enable: _toggle_root_folder(view, ns, True))
        divider()
        # Group: Nexus - each item shows only when it has valid targets (Tk).
        _endorse_multi = [nm for nm in _names
                          if _has_nexus_id(view, nm) and not _is_endorsed(view, nm)]
        _abstain_multi = [nm for nm in _names
                          if _has_nexus_id(view, nm) and _is_endorsed(view, nm)]
        _check_multi = [nm for nm in _names
                        if _has_nexus_id(view, nm) or bool(_modio_url(view, nm))]
        _nexus_multi = [nm for nm in _names if _has_nexus_page(view, nm)]
        _reqs_multi = [nm for nm in _names if _has_missing_reqs(view, nm)]
        _qu = [nm for nm in _names if _has_update_flag(view, nm)]
        # Reinstall: archive on disk OR redownloadable from Nexus/Thunderstore.
        _reinstall_multi = [nm for nm in _names
                            if _installation_archive(view, nm) is not None
                            or _can_redownload(view, nm)]
        # Endorse/version/check/track/open-on-Nexus nest under "Nexus Actions".
        _track_multi = [nm for nm in _names if _has_nexus_id(view, nm)]
        _nexus_sub = []
        if _abstain_multi:
            _nexus_sub.append(
                (_mtf("Abstain selected ({0})", len(_abstain_multi)),
                 lambda ns=_abstain_multi: _endorse(view, ns, False)))
        if _check_multi:
            _nexus_sub.append(
                (_mtf("Check Updates ({0})", len(_check_multi)),
                 lambda ns=_check_multi: _check_updates(view, ns)))
        if _endorse_multi:
            _nexus_sub.append(
                (_mtf("Endorse selected ({0})", len(_endorse_multi)),
                 lambda ns=_endorse_multi: _endorse(view, ns, True)))
        if _track_multi:
            _nexus_sub.append(
                (_mtf("Track Mod ({0})", len(_track_multi)),
                 lambda ns=_track_multi: _track(view, ns)))
        if _nexus_multi:
            _nexus_sub.append(
                (_mtf("Open on Nexus ({0})", len(_nexus_multi)),
                 lambda ns=_nexus_multi: _open_on_nexus_multi(view, ns)))
        if _nexus_sub:
            submenu(_mt("Nexus Actions"), _nexus_sub)
        if _reqs_multi:
            act(_mtf("Missing Requirements ({0})", len(_reqs_multi)),
                lambda ns=_reqs_multi: _missing_reqs(view, ns))
        if _qu:
            act(_mtf("Quick Update ({0})", len(_qu)),
                lambda ns=_qu: _quick_update(view, ns))
        if _reinstall_multi:
            act(_mtf("Reinstall ({0})", len(_reinstall_multi)),
                lambda ns=_reinstall_multi: _reinstall(view, ns))
        divider()
        # Group: organise
        _others = _other_profiles(view)
        if _others:
            submenu(_mtf("Copy to profile ({0})", n),
                    _profile_submenu_items(view, _names, sel_mods, _others, False))
            submenu(_mtf("Move to profile ({0})", n),
                    _profile_submenu_items(view, _names, sel_mods, _others, True))
        act(_mtf("Disable selected ({0})", n),
            lambda: _set_enabled(view, model, sel_mods, False),
            shortcut=_shortcut_hint("toggle_selected"))
        act(_mtf("Enable selected ({0})", n),
            lambda: _set_enabled(view, model, sel_mods, True),
            shortcut=_shortcut_hint("toggle_selected"))
        if _separator_choices(model):
            submenu(_mtf("Move to separator ({0})", n),
                    _separator_submenu_items(view, model, sel_mods),
                    scroll_cap=10)
        if len(sel_mods) >= 2:
            act(_mtf("Sort Alphabetically ({0})", n),
                lambda: _sort_selected_alphabetically(view, model, sel_mods))
        divider()
        # Group: notes
        act(_mtf("Add note ({0})", n), lambda: _open_note_editor(view, _names))
        _note_remove = [nm for nm in _names if _mod_note(view, nm)]
        if _note_remove:
            act(_mtf("Remove note ({0})", len(_note_remove)),
                lambda ns=_note_remove: _remove_notes(view, ns))
        divider()
        # Group: remove
        act(_mtf("Remove mod ({0})", n),
            lambda: _remove_mods_multi(view, model, sel_mods),
            shortcut=_shortcut_hint("remove"))
        return

    locked = entry.locked
    name = entry.name
    _staging_ok = getattr(view, "staging_dir", None) is not None
    # Group 1: manage
    act(_mt("Open folder"), lambda: _open_folder(view, model, row))
    # Bundle options… - shown only when the mod carries a RE/Fluffy bundle spec.
    if _has_bundle_spec(view, name):
        act(_mt("Bundle options…"), lambda: _open_bundle(view, name))
    if _staging_ok:
        act(_mt("Create empty mod below"), lambda: _create_empty_mod(view, model, row))
    # Reinstall Mod - from the recorded archive when it's still on disk (Tk:
    # ctx_meta present + _find_installation_archive), reinstalling into the same
    # folder (silent Replace-All). When the archive is gone but the mod carries
    # a Nexus mod/file id or Thunderstore package identity, offer 'Reinstall
    # (Redownload)' instead. Nexus keeps its premium/manual-browser behaviour;
    # Thunderstore downloads are public and direct.
    if _installation_archive(view, name) is not None:
        act(_mt("Reinstall Mod"), lambda: _reinstall(view, [name]))
    elif _can_redownload(view, name):
        act(_mt("Reinstall (Redownload)"), lambda: _reinstall(view, [name]))
    act(_mt("Rename mod"), lambda: _rename(view, model, row), enabled=not locked,
        shortcut=_shortcut_hint("rename"))
    divider()
    # Group 2: files & install options
    if _staging_ok:
        _is_rf = _is_root_folder(view, name)
        act(_mt("Disable Root Folder install") if _is_rf else _mt("Enable Root Folder install"),
            lambda: _toggle_root_folder(view, [name], not _is_rf))
    divider()
    # Group 3: Nexus / online & updates - each item shows only when applicable.
    # The endorse/version/check/track/open-on-Nexus items nest under a
    # "Nexus Actions" submenu; the rest stay inline.
    _endorsed = _is_endorsed(view, name)
    _has_id = _has_nexus_id(view, name)
    _nexus_items = []
    if _has_id:
        _nexus_items.append(
            (_mt("Abstain from Endorsement") if _endorsed else _mt("Endorse Mod"),
             lambda: _endorse(view, [name], not _endorsed)))
        _nexus_items.append(
            (_mt("Change Version"), lambda: _change_version(view, name)))
    if _has_id or bool(_modio_url(view, name)):
        _nexus_items.append(
            (_mt("Check Updates"), lambda: _check_updates(view, [name])))
    if _has_id:
        _nexus_items.append(
            (_mt("Track Mod"), lambda: _track(view, [name])))
    if _has_nexus_page(view, name):
        _nexus_items.append(
            (_mt("Open on Nexus"), lambda: _open_on_nexus(view, name)))
    if _nexus_items:
        submenu(_mt("Nexus Actions"), _nexus_items)
    # Thunderstore mods get their own submenu, mirroring "Nexus Actions".
    # A mod can legitimately carry both sections (same mod mirrored on both
    # stores), so this is independent of the Nexus block above.
    if _is_thunderstore_mod(view, name):
        submenu(_mt("Thunderstore Actions"), [
            (_mt("Change Version"),
             lambda: _thunderstore_change_version(view, name)),
            (_mt("Check Updates"),
             lambda: _thunderstore_check_updates(view, [name])),
            (_mt("Open on Thunderstore"),
             lambda: _open_on_thunderstore(view, name)),
        ])
    if _modio_url(view, name):
        act(_mt("Open on mod.io"), lambda: _open_on_modio(view, name))
    if _has_update_flag(view, name):
        act(_mt("Quick Update"), lambda: _quick_update(view, [name]))
    divider()
    # Group 4: organise / layout
    act(_mt("Add separator above"), lambda: _add_separator(view, model, row, True))
    act(_mt("Add separator below"), lambda: _add_separator(view, model, row, False))
    _others = _other_profiles(view)
    if _others:
        submenu(_mt("Copy to profile"),
                _profile_submenu_items(view, [name], [row], _others, False))
        submenu(_mt("Move to profile"),
                _profile_submenu_items(view, [name], [row], _others, True))
    if not locked and _separator_choices(model):
        submenu(_mt("Move to separator"),
                _separator_submenu_items(view, model, [row]),
                scroll_cap=10)
    if not locked:
        act(_mt("Set priority…"), lambda: _set_priority(view, model, row))
    divider()
    # Group 5: info / conflicts / notes
    _has_note = bool(_mod_note(view, name))
    act(_mt("Edit note") if _has_note else _mt("Add note"),
        lambda: _open_note_editor(view, [name]))
    if _nif_viewer_available(view) and _has_meshes(view, name):
        act(_mt("Open in NIF Viewer"), lambda: _open_nif_viewer(view, name))
    if _has_fomod_choices(view, name):
        act(_mt("View FOMOD Choices"), lambda: _show_fomod_choices(view, name))
    if _has_conflict(model, row):
        act(_mt("Show Conflicts"), lambda: _show_conflicts(view, name))
        act(_mt("Clear Conflict Filter") if _conflict_filter_on(view, name)
            else _mt("Filter Conflicts"),
            lambda: _filter_conflicts(view, name))
    elif _conflict_filter_on(view):
        # Conflict-free rows can't start the filter, but they stay reachable
        # while it's on (they show up inside a separator block that has a
        # match) - so keep the off switch on them too.
        act(_mt("Clear Conflict Filter"), lambda: _clear_conflict_filter(view))
    if _has_missing_reqs(view, name):
        act(_mt("Missing Requirements"), lambda: _missing_reqs(view, [name]))
    if _has_id:
        act(_mt("View Requirements"), lambda: _view_requirements(view, name))
    divider()
    # Group 6: remove
    act(_mt("Remove mod"), lambda: _remove(view, model, row), enabled=not locked,
        shortcut=_shortcut_hint("remove"))


def _fill_scroll_submenu(root_menu, sub, items, scroll_cap):
    """Fill *sub* with a single QWidgetAction wrapping a search box over a
    QListWidget showing *items* - visible height capped at *scroll_cap* rows,
    real scrollbar past that, and typing in the box filters the rows. Enter
    picks the highlighted (else first visible) row; clicking a row or pressing
    Enter closes the whole menu and runs its slot."""
    from PySide6.QtCore import QEvent, QObject, Qt, QTimer
    from PySide6.QtWidgets import (QLineEdit, QListWidget, QVBoxLayout,
                                   QWidget, QWidgetAction)
    box = QWidget(sub)
    lay = QVBoxLayout(box)
    lay.setContentsMargins(4, 4, 4, 4)
    lay.setSpacing(4)
    edit = QLineEdit(box)
    edit.setPlaceholderText(_mt("Search…"))
    edit.setClearButtonEnabled(True)
    lst = QListWidget(box)
    lst.setFrameShape(QListWidget.Shape.NoFrame)
    lst.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    lst.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
    lst.setMouseTracking(True)
    # Focus stays in the search box; arrows are forwarded so the list never
    # needs it (and hover/selection styling still applies without focus).
    lst.setFocusPolicy(Qt.FocusPolicy.NoFocus)
    # Menu-like hover highlight (QListWidget only tints on selection by default).
    lst.setStyleSheet(
        "QListWidget { background: transparent; outline: none; }"
        "QListWidget::item { padding: 3px 16px; }"
        "QListWidget::item:hover, QListWidget::item:selected {"
        " background: palette(highlight); color: palette(highlighted-text); }")
    lay.addWidget(edit)
    lay.addWidget(lst)
    slots = []
    for text, slot in items:
        # Item texts are DATA (separator names), not translated.
        lst.addItem(text)
        slots.append(slot)
    row_h = lst.sizeHintForRow(0) or 22
    lst.setFixedHeight(row_h * scroll_cap + 4)
    sbar_w = lst.verticalScrollBar().sizeHint().width()
    lst.setMinimumWidth(lst.sizeHintForColumn(0) + sbar_w + 8)

    def _pick(row):
        sub.close()
        root_menu.close()
        if 0 <= row < len(slots):
            slots[row]()

    lst.itemClicked.connect(lambda item: _pick(lst.row(item)))

    def _visible_rows():
        return [i for i in range(lst.count()) if not lst.item(i).isHidden()]

    def _apply_filter(text):
        needle = text.casefold()
        for i in range(lst.count()):
            lst.item(i).setHidden(needle not in lst.item(i).text().casefold())
        vis = _visible_rows()
        # Keep a current row so Enter always has a target while typing.
        lst.setCurrentRow(vis[0] if vis else -1)

    edit.textChanged.connect(_apply_filter)

    def _pick_current():
        row = lst.currentRow()
        if row < 0 or lst.item(row).isHidden():
            vis = _visible_rows()
            row = vis[0] if vis else -1
        if row >= 0:
            _pick(row)

    edit.returnPressed.connect(_pick_current)

    def _step(delta):
        vis = _visible_rows()
        if not vis:
            return
        cur = lst.currentRow()
        pos = vis.index(cur) if cur in vis else (-1 if delta > 0 else 0)
        lst.setCurrentRow(vis[max(0, min(len(vis) - 1, pos + delta))])
        lst.scrollToItem(lst.currentItem())

    class _ArrowFwd(QObject):
        # Up/Down typed in the search box move the list highlight.
        def eventFilter(self, obj, ev):
            if ev.type() == QEvent.Type.KeyPress:
                if ev.key() == Qt.Key.Key_Up:
                    _step(-1)
                    return True
                if ev.key() == Qt.Key.Key_Down:
                    _step(1)
                    return True
            return False

    edit.installEventFilter(_ArrowFwd(edit))
    # QMenu doesn't focus embedded widgets on its own - grab it post-show so
    # typing filters immediately (singleShot: the menu isn't visible yet inside
    # aboutToShow, and setFocus on a hidden widget is dropped).
    sub.aboutToShow.connect(lambda: QTimer.singleShot(0, edit.setFocus))
    wa = QWidgetAction(sub)
    wa.setDefaultWidget(box)
    sub.addAction(wa)


def _boundary_names():
    from gui_qt.modlist_model import _BOUNDARY_NAMES
    return _BOUNDARY_NAMES


# ---- action implementations (model-level; backend ops come later) ---------

def _set_enabled(view, model, rows, state):
    # One save + one enabled_changed for the whole selection (toggle() per row
    # would write modlist.txt and re-sync plugins N times).
    model.set_rows_enabled(rows, state)


def _open_folder(view, model, row):
    """Open the row's on-disk folder via the platform opener (Utils.environment.xdg).

    Uses the view's _resolve_entry_folder so the synthetic Overwrite /
    Root Folder rows open their effective deploy paths, not staging/<name>."""
    path = None
    resolver = getattr(view, "_resolve_entry_folder", None)
    if callable(resolver):
        path = resolver(row)
    if path is None:
        staging = getattr(view, "staging_dir", None)
        if staging is None:
            return
        path = staging / model.entry(row).name
    try:
        from Utils.environment.xdg import xdg_open
        # Surface opener failures - the whole chain can fail on the host side
        # (no file-manager association) and would otherwise be silent.
        xdg_open(str(path), log_fn=lambda m: _notify(view, m, "warning"))
    except Exception as e:
        _notify(view, f"Open folder failed: {e}", "warning")


def _check_updates(view, names):
    """Run a Nexus update check limited to *names* (the window installs the
    callback in _reload_modlist). No-op if it isn't wired (e.g. headless)."""
    cb = getattr(view, "on_check_updates", None)
    if cb is not None and names:
        cb(set(names))


def _change_version(view, name):
    """Open the Change Version picker for *name* (the window installs the
    callback in _reload_modlist). No-op if it isn't wired (e.g. headless)."""
    cb = getattr(view, "on_change_version", None)
    if cb is not None and name:
        cb(name)


def _open_bundle(view, name):
    """Open the Bundle Options selector for *name* (the window installs the
    callback in _reload_modlist). No-op if it isn't wired (e.g. headless)."""
    cb = getattr(view, "on_bundle_options", None)
    if cb is not None and name:
        cb(name)


def _has_update_flag(view, name: str) -> bool:
    """True if *name* currently carries the pending-update flag (FLAG_UPDATE),
    i.e. Check Updates found a newer file and it isn't ignored. Read straight
    off the model's flag bitmask so it matches what the row paints."""
    try:
        model = view.model()
    except Exception:
        return False
    from gui_qt.modlist_data import FLAG_UPDATE
    bits = model._flags.get(name, 0) if hasattr(model, "_flags") else 0
    return bool(bits & FLAG_UPDATE)


def _has_missing_reqs(view, name: str) -> bool:
    """True if *name* carries the missing-requirements flag (FLAG_MISSING_REQS).
    Read off the model's flag bitmask (same source the row paints), so the menu
    matches Tk's `mod_name in self._missing_reqs`.

    Fallback: a mod whose every missing requirement is individually ignored
    (meta.ini ignoredRequirements) has no ⚠ flag, but the panel must stay
    reachable so the ignores can be unticked - keep the menu item when the meta
    carries per-requirement ignores."""
    try:
        model = view.model()
    except Exception:
        return False
    from gui_qt.modlist_data import FLAG_MISSING_REQS
    bits = model._flags.get(name, 0) if hasattr(model, "_flags") else 0
    if bits & FLAG_MISSING_REQS:
        return True
    meta = _read_mod_meta(view, name)
    return bool(meta is not None
                and getattr(meta, "ignored_requirements", ""))


def _quick_update(view, names):
    """Auto-install the latest name-matched version for each update-flagged mod
    in *names* (the window installs the callback in _reload_modlist). No-op if
    it isn't wired (e.g. headless)."""
    cb = getattr(view, "on_quick_update", None)
    targets = [n for n in names if _has_update_flag(view, n)]
    if cb is not None and targets:
        cb(targets)


def _reinstall(view, names):
    """Reinstall each mod in *names* from its recorded install archive (the
    window installs the callback in _reload_modlist). Mods whose archive is gone
    are skipped by the handler. No-op if it isn't wired (e.g. headless)."""
    cb = getattr(view, "on_reinstall", None)
    if cb is not None and names:
        cb(list(names))


def _missing_reqs(view, names):
    """Open the Missing Requirements panel for *names* (1 = single, N = multi).
    The window installs the callback in _reload_modlist; no-op if unwired."""
    cb = getattr(view, "on_missing_reqs", None)
    if cb is not None and names:
        cb(names[0] if len(names) == 1 else set(names))


def _view_requirements(view, name):
    """Open the View Requirements tab for *name* (the window installs the
    callback in _reload_modlist). No-op if it isn't wired (e.g. headless)."""
    cb = getattr(view, "on_view_requirements", None)
    if cb is not None and name:
        cb(name)


def _has_conflict(model, row) -> bool:
    """True if the row has a loose OR BSA conflict (so Show Conflicts is useful)."""
    from gui_qt.modlist_model import COL_CONFLICTS, ConflictRole, BsaConflictRole
    idx = model.index(row, COL_CONFLICTS)
    loose = model.data(idx, ConflictRole) or 0
    bsa = model.data(idx, BsaConflictRole) or 0
    return bool(loose) or bool(bsa)


def _show_conflicts(view, name):
    """Open the Show Conflicts tab for *name* (window installs the callback in
    _reload_modlist). No-op if it isn't wired (e.g. headless)."""
    cb = getattr(view, "on_show_conflicts", None)
    if cb is not None and name:
        cb(name)


def _filter_conflicts(view, name):
    """Narrow the modlist to *name* + the mods it conflicts with (window installs
    the callback in _reload_modlist). No-op if it isn't wired (e.g. headless)."""
    cb = getattr(view, "on_filter_conflicts", None)
    if cb is not None and name:
        cb(name)


def _clear_conflict_filter(view):
    """Drop the Filter Conflicts narrowing. No-op if it isn't wired."""
    cb = getattr(view, "on_clear_conflict_filter", None)
    if cb is not None:
        cb()


def _conflict_filter_on(view, name: str | None = None) -> bool:
    """True while a Filter Conflicts narrowing is active - and, when *name* is
    given, only if it's anchored on that mod (so its menu entry flips to Clear
    instead of re-filtering to the same set)."""
    cb = getattr(view, "conflict_filter_anchor", None)
    if cb is None:
        return False
    anchor = cb() or ""
    return anchor == name if name is not None else bool(anchor)


def _has_fomod_choices(view, name: str) -> bool:
    """True when this mod was installed through the FOMOD wizard and its
    selections were saved, so there is something to show."""
    if not name:
        return False
    try:
        from Utils.fomod.choices import has_choices
        game = getattr(view, "game", None)
        return has_choices(name, getattr(view, "profile_dir", None),
                           getattr(game, "name", "") or "")
    except Exception:
        return False


def _show_fomod_choices(view, name):
    """Open the FOMOD Choices tab for *name* (window installs the callback in
    _reload_modlist). No-op if it isn't wired (e.g. headless)."""
    cb = getattr(view, "on_show_fomod_choices", None)
    if cb is not None and name:
        cb(name)


def _nif_viewer_available(view) -> bool:
    """True when the active game ships the NIF Viewer tool (the Bethesda
    titles). Asked of the tool registry so the two can't drift apart."""
    game_id = getattr(getattr(view, "game", None), "game_id", "") or ""
    if not game_id:
        return False
    try:
        from Utils.wizards.plugins import get_builtin_wizard_tools_for_game
        return any(t.id == "nif_viewer"
                   for t in get_builtin_wizard_tools_for_game(game_id))
    except Exception:
        return False


def _has_meshes(view, name: str) -> bool:
    """True if the mod ships at least one .nif (loose or in its own BSA/BA2)."""
    staging = getattr(view, "staging_dir", None)
    if staging is None:
        return False
    try:
        from Utils.assets.catalog import mod_has_assets
        return mod_has_assets(staging, name, game=getattr(view, "game", None))
    except Exception:
        return False


def _open_nif_viewer(view, name):
    """Open the NIF Viewer tab scoped to *name* (the window installs the
    callback in _reload_modlist). No-op if it isn't wired (e.g. headless)."""
    cb = getattr(view, "on_open_nif_viewer", None)
    if cb is not None and name:
        cb(name)


def _mod_nexus_url(view, name: str) -> str:
    """The mod's Nexus page, with the game's primary as a legacy fallback."""
    staging = getattr(view, "staging_dir", None)
    if staging is None:
        return ""
    meta_path = staging / name / "meta.ini"
    if not meta_path.is_file():
        return ""
    try:
        from Nexus.nexus_meta import normalise_game_domain, read_meta
        meta = read_meta(meta_path)
        domain = normalise_game_domain(meta.game_domain)
        if not domain:
            game = getattr(view, "game", None)
            domain = normalise_game_domain(
                getattr(game, "nexus_game_domain", "") or "")
        if domain and int(getattr(meta, "mod_id", 0) or 0) > 0:
            return f"https://www.nexusmods.com/{domain}/mods/{meta.mod_id}"
        return meta.nexus_page_url or ""
    except Exception:
        return ""


def _has_nexus_page(view, name: str) -> bool:
    return bool(_mod_nexus_url(view, name))


def _open_on_nexus(view, name: str):
    url = _mod_nexus_url(view, name)
    if not url:
        return
    try:
        from Utils.environment.xdg import open_url
        open_url(url)
    except Exception:
        pass


def _modio_url(view, name: str) -> str:
    """The mod's stored mod.io profile URL from meta.ini ("" if none). The
    slug-based URL is captured at install/update time (BG3 mod.io mods)."""
    staging = getattr(view, "staging_dir", None)
    if staging is None:
        return ""
    meta_path = staging / name / "meta.ini"
    if not meta_path.is_file():
        return ""
    try:
        import configparser
        cp = configparser.ConfigParser(interpolation=None)
        cp.read(str(meta_path), encoding="utf-8")
        if cp.has_option("modio", "profileUrl"):
            return (cp.get("modio", "profileUrl", fallback="") or "").strip()
        return (cp.get(
            "General", "modioProfileUrl", fallback="") or "").strip()
    except Exception:
        return ""


def _open_on_modio(view, name: str):
    url = _modio_url(view, name)
    if not url:
        return
    try:
        from Utils.environment.xdg import open_url
        open_url(url)
    except Exception:
        pass


# ---- Thunderstore ----------------------------------------------------------
def _thunderstore_meta(view, name: str):
    """The mod's parsed [thunderstore] metadata, or None when it isn't one."""
    staging = getattr(view, "staging_dir", None)
    if staging is None:
        return None
    meta_path = staging / name / "meta.ini"
    if not meta_path.is_file():
        return None
    try:
        from Thunderstore.thunderstore_meta import read_meta
        meta = read_meta(meta_path)
    except Exception:
        return None
    return meta if meta.package_id else None


def _is_thunderstore_mod(view, name: str) -> bool:
    return _thunderstore_meta(view, name) is not None


def _thunderstore_url(view, name: str) -> str:
    """The mod's Thunderstore page URL ("" if it isn't a Thunderstore mod).

    Only the community-scoped ``/c/{community}/p/{ns}/{name}/`` form actually
    resolves - the bare ``/package/{ns}/{name}/`` URL the API reports as
    ``package_url`` 404s in a browser (verified 2026-08-11). So a mod whose
    community was never recorded returns "" here, and the caller resolves it
    from the API instead of opening a dead link.
    """
    meta = _thunderstore_meta(view, name)
    if meta is None:
        return ""
    community = (meta.community or "").strip()
    if not community:
        return ""
    return (f"https://thunderstore.io/c/{community}/p/"
            f"{meta.namespace}/{meta.name}/")


def _open_on_thunderstore(view, name: str):
    """Open the mod's Thunderstore page, resolving its community if needed."""
    url = _thunderstore_url(view, name)
    if not url:
        # No community stored (installed before it was recorded, or the stamp
        # failed): look it up now and remember it, so the next click is instant.
        meta = _thunderstore_meta(view, name)
        if meta is None:
            return
        try:
            from Thunderstore.ror2mm_handler import Ror2mmLink
            from Thunderstore.thunderstore_download import resolve_communities
            from Thunderstore.thunderstore_meta import write_meta
            slugs = resolve_communities(Ror2mmLink(
                namespace=meta.namespace, name=meta.name,
                version=meta.version or "0.0.0"))
            if not slugs:
                return
            meta.community = slugs[0]
            staging = getattr(view, "staging_dir", None)
            if staging is not None:
                write_meta(staging / name / "meta.ini", meta)
            url = (f"https://thunderstore.io/c/{meta.community}/p/"
                   f"{meta.namespace}/{meta.name}/")
        except Exception:
            return
    try:
        from Utils.environment.xdg import open_url
        open_url(url)
    except Exception:
        pass


def _thunderstore_change_version(view, name: str):
    """Open the Thunderstore version picker (host installs the callback)."""
    cb = getattr(view, "on_thunderstore_change_version", None)
    if cb is not None and name:
        cb(name)


def _thunderstore_check_updates(view, names):
    """Run a Thunderstore-only update check over *names*."""
    cb = getattr(view, "on_thunderstore_check_updates", None)
    if cb is not None and names:
        cb(set(names))


# ---- Move to separator -----------------------------------------------------
def _separator_choices(model):
    """(display, internal_name) for every non-boundary separator, in list order."""
    from gui_qt.modlist_model import _BOUNDARY_NAMES
    out = []
    for r in range(model.rowCount()):
        e = model.entry(r)
        if e.is_separator and e.name not in _BOUNDARY_NAMES:
            out.append((e.display_name, e.name))
    return out


def _separator_submenu_items(view, model, mod_rows):
    """(display, slot) pairs for the Move-to-separator submenu - one entry per
    non-boundary separator, sorted A→Z by display name (the natural list order
    isn't useful in a menu). Separator display names are DATA, not translated."""
    choices = sorted(_separator_choices(model), key=lambda c: c[0].casefold())
    return [
        (display,
         lambda sep=internal: _move_to_separator(view, model, mod_rows, sep))
        for display, internal in choices
    ]


def _move_to_separator(view, model, mod_rows, sep_name):
    """Reposition the selected mods directly below *sep_name* (lowest-priority end
    of its group in the reverse-priority display, matching Tk). Rebuilds the body
    without the moved mods, then inserts them right after the separator."""
    from gui_qt.modlist_model import _PINNED_NAMES
    rows = sorted(r for r in mod_rows
                  if not model.entry(r).is_separator
                  and model.entry(r).name not in _PINNED_NAMES)
    if not rows:
        return
    moved_names = {model.entry(r).name for r in rows}
    # Take the block in NATURAL order, not display order - the display may be
    # an inverted/sorted permutation (reverse-priority mode reverses it), and
    # splicing a display-ordered block into the natural list flips the mods'
    # relative priorities (GH#380).
    moved = [e for e in model.natural_entries() if e.name in moved_names]
    from Utils.diagnostics.conflicts import ConflictTimeline
    timing = ConflictTimeline("move", [e.name for e in moved])
    phase_started = timing.now()
    old_order = [e.name for e in model.natural_entries() if not e.is_separator]
    # Body = the NATURAL order minus the moved mods - the display may be a
    # sorted/inverted permutation and must never be persisted as the new order.
    body = [e for e in model.natural_entries()
            if e.name not in _PINNED_NAMES and e.name not in moved_names]
    sep_idx = next((i for i, e in enumerate(body)
                    if e.is_separator and e.name == sep_name), None)
    if sep_idx is None:
        return
    body[sep_idx + 1:sep_idx + 1] = moved
    model.set_entries(body)
    # Pure reorder - hand the app a "move" ctx so a repositioning that crossed
    # no conflicting mod skips the conflict rebuild (see app._on_modlist_saved).
    new_order = [e.name for e in model.natural_entries() if not e.is_separator]
    ctx = model._move_ctx(old_order, new_order, [e.name for e in moved])
    timing.mark("move-to-separator Qt model update complete",
                phase_started=phase_started)
    try:
        save_ctx = (("move",) + ctx + (timing,) if ctx is not None
                    else ("full", timing))
        model.save(edit_ctx=save_ctx)
    except Exception as exc:
        timing.finish(f"move-to-separator save failed: {exc}")


# ---- Copy / Move to profile ------------------------------------------------
def _other_profiles(view):
    """Profile names for this game, excluding the current one and Profile
    Groups ([] if none). Groups are excluded as TARGETS: 'copy into a group'
    is ill-defined (which member would own it?) - copy into a member and the
    group reconciles it in."""
    game = getattr(view, "game", None)
    pdir = getattr(view, "profile_dir", None)
    if game is None or pdir is None:
        return []
    try:
        from Utils.games.registry import _profiles_for_game
        from Utils.profiles.groups import is_group
        cur = pdir.name
        profiles_dir = pdir.parent
        return [p for p in _profiles_for_game(game.name)
                if p != cur and not is_group(profiles_dir / p)]
    except Exception:
        return []


def _profile_submenu_items(view, names, mod_rows, others, move: bool):
    """Build the (profile_name, slot) list for the Copy/Move-to-profile submenu.
    Each entry copies/moves *names* to that profile (Tk lists the profiles as a
    submenu rather than opening a picker window)."""
    model = view.model()
    # The copy worker registers the block in the target modlist assuming
    # highest-priority-first (app._run_copy_to_profile prepends it as one
    # unit) - reorder the display-ordered selection into NATURAL order so a
    # reverse-priority (or column-sorted) view doesn't flip the block.
    nat = {e.name: i for i, e in enumerate(model.natural_entries())}
    names = sorted(names, key=lambda n: nat.get(n, len(nat)))
    enabled_map = {}
    for r in mod_rows:
        e = model.entry(r)
        if not e.is_separator:
            enabled_map[e.name] = e.enabled
    # The label is display-only; the callback closes over the FOLDER name.
    return [
        (profile_display(prof), (lambda p=prof: _copy_to_profile(
            view, names, dict(enabled_map), p, move)))
        for prof in others
    ]


def _copy_to_profile(view, names, enabled_map, target_profile, move):
    """Delegate the copy/move to the window (needs game, worker thread, collision
    overlay, and - for move - remove_mods + reload)."""
    cb = getattr(view, "on_copy_to_profile", None)
    if cb is not None and names and target_profile:
        cb(list(names), dict(enabled_map), target_profile, move)


def _read_mod_meta(view, name):
    """Read a mod's meta.ini (or None). Central so the menu helpers agree.
    Memoised per menu build (the gate helpers each want the same meta 5-7
    times per selected mod; build_context_menu resets the memo)."""
    cache = getattr(view, "_menu_meta_cache", None)
    if cache is not None and name in cache:
        return cache[name]
    meta = None
    staging = getattr(view, "staging_dir", None)
    if staging is not None:
        meta_path = staging / name / "meta.ini"
        if meta_path.is_file():
            try:
                from Nexus.nexus_meta import read_meta
                meta = read_meta(meta_path)
            except Exception:
                meta = None
    if cache is not None:
        cache[name] = meta
    return meta


def _has_bundle_spec(view, name: str) -> bool:
    """True if the mod carries an RE/Fluffy bundle spec (Tk `_bundle_spec_path`).
    Read off the model's FLAG_BUNDLE bit (computed by read_meta_for_entries from
    the same meta.ini) instead of re-parsing the spec on every right-click."""
    try:
        model = view.model()
    except Exception:
        return False
    from gui_qt.modlist_data import FLAG_BUNDLE
    bits = model._flags.get(name, 0) if hasattr(model, "_flags") else 0
    return bool(bits & FLAG_BUNDLE)


def _installation_archive(view, name: str):
    """Path to the mod's original install archive if it still exists, else None
    (Tk `_find_installation_archive`). Gates the (still-unwired) Reinstall Mod
    item. Searches the user's Downloads dir + the game's configured caches +
    any extra download locations, matching the Tk lookup."""
    meta = _read_mod_meta(view, name)
    filenames = []
    nexus_filename = (
        getattr(meta, "installation_file", "") if meta is not None else "")
    if nexus_filename:
        filenames.append(nexus_filename)
    ts_meta = _thunderstore_meta(view, name)
    if ts_meta is not None and ts_meta.namespace and ts_meta.name \
            and ts_meta.version:
        ts_full_name = (ts_meta.full_name or
                        f"{ts_meta.namespace}-{ts_meta.name}-{ts_meta.version}")
        ts_filename = f"{ts_full_name}.zip"
        if ts_filename not in filenames:
            filenames.append(ts_filename)
    if not filenames:
        return None
    from pathlib import Path
    game = getattr(view, "game", None)
    game_name = getattr(game, "name", "") or ""
    search_dirs = []
    try:
        from Utils.config_paths import list_all_cache_dirs
        from Utils.downloads.locations import (
            get_default_downloads_dir, is_default_downloads_disabled,
            load_extra_download_locations)
        if not is_default_downloads_disabled():
            search_dirs.append(get_default_downloads_dir())
        search_dirs.extend(list_all_cache_dirs(game_name))
        search_dirs.extend(Path(p) for p in load_extra_download_locations())
    except Exception:
        return None
    for d in search_dirs:
        for filename in filenames:
            cand = Path(d) / filename
            if cand.is_file():
                return cand
    return None


def _can_redownload(view, name: str) -> bool:
    """Whether a missing install archive can be fetched from a recorded store.

    Nexus requires mod/file ids plus a game domain. Thunderstore packages are
    public and need only their namespace, name and exact installed version.
    """
    meta = _read_mod_meta(view, name)
    if meta is not None:
        mod_id = int(getattr(meta, "mod_id", 0) or 0)
        file_id = int(getattr(meta, "file_id", 0) or 0)
        if mod_id > 0 and file_id > 0:
            from Nexus.nexus_meta import normalise_game_domain
            domain = normalise_game_domain(
                getattr(meta, "game_domain", "") or "")
            if not domain:
                game = getattr(view, "game", None)
                domain = getattr(game, "nexus_game_domain", "") or ""
            if domain:
                return True
    ts_meta = _thunderstore_meta(view, name)
    return bool(ts_meta is not None and ts_meta.namespace and ts_meta.name
                and ts_meta.version)


# ---- Root Folder install toggle -------------------------------------------
def _is_root_folder(view, name) -> bool:
    m = _read_mod_meta(view, name)
    return bool(getattr(m, "root_folder", False)) if m is not None else False


def _toggle_root_folder(view, names, enable: bool):
    """Set rootFolder=enable in each mod's meta.ini (skips ones already there),
    then ask the window to rescan + rebuild the filemap (the index caches
    strip-applied vs verbatim paths). Port of Tk _set_root_folder_flag_multi."""
    staging = getattr(view, "staging_dir", None)
    if staging is None:
        return
    from Nexus.nexus_meta import read_meta, write_meta, NexusModMeta
    changed = []
    for nm in names:
        meta_path = staging / nm / "meta.ini"
        try:
            meta = read_meta(meta_path) if meta_path.is_file() else NexusModMeta()
            if bool(meta.root_folder) == enable:
                continue
            meta.root_folder = enable
            meta_path.parent.mkdir(parents=True, exist_ok=True)
            write_meta(meta_path, meta)
            changed.append(nm)
        except Exception:
            continue
    if changed:
        cb = getattr(view, "on_root_folder_changed", None)
        if cb is not None:
            cb(changed)


# ---- Endorse / Abstain -----------------------------------------------------
def _is_endorsed(view, name) -> bool:
    m = _read_mod_meta(view, name)
    return bool(getattr(m, "endorsed", False)) if m is not None else False


def _has_nexus_id(view, name) -> bool:
    m = _read_mod_meta(view, name)
    return bool(getattr(m, "mod_id", 0)) if m is not None else False


def _endorse(view, names, endorse: bool):
    """Endorse/abstain the mods - delegated to the window (needs the shared
    Nexus API + a worker thread; see app._on_modlist_endorse)."""
    cb = getattr(view, "on_endorse", None)
    if cb is not None and names:
        cb(list(names), endorse)


def _track(view, names):
    """Start tracking the mods on Nexus - delegated to the window (needs the
    shared Nexus API + a worker thread; see app._on_modlist_track)."""
    cb = getattr(view, "on_track", None)
    if cb is not None and names:
        cb(list(names))


# ---- Notes -----------------------------------------------------------------
def _profile_notes(view):
    """(profile_dir, {name: note}) for the active profile, or (None, {})."""
    pdir = getattr(view, "profile_dir", None)
    if pdir is None:
        return None, {}
    try:
        from Utils.profiles.state import read_mod_notes
        return pdir, read_mod_notes(pdir)
    except Exception:
        return pdir, {}


def _mod_note(view, name) -> str:
    _pdir, notes = _profile_notes(view)
    return notes.get(name, "")


def _open_note_editor(view, names):
    """Open the note editor for one mod (existing text) or many (append on
    save). Port of Tk _open_note_editor_by_name / _for_multi."""
    pdir, notes = _profile_notes(view)
    if pdir is None or not names:
        return
    from Utils.profiles.state import write_mod_notes
    single = len(names) == 1
    title = names[0] if single else f"{len(names)} mods"
    initial = notes.get(names[0], "") if single else ""

    def _save(text: str):
        text = (text or "").strip()
        cur = dict(notes)
        if single:
            if text:
                cur[names[0]] = text
            else:
                cur.pop(names[0], None)
        else:
            if not text:
                return
            for nm in names:
                existing = cur.get(nm, "").rstrip()
                cur[nm] = f"{existing}\n{text}" if existing else text
        try:
            write_mod_notes(pdir, cur)
        except Exception:
            pass
        cb = getattr(view, "on_notes_changed", None)
        if cb is not None:
            cb(list(names))

    def _remove():
        cur = dict(notes)
        for nm in names:
            cur.pop(nm, None)
        try:
            write_mod_notes(pdir, cur)
        except Exception:
            pass
        cb = getattr(view, "on_notes_changed", None)
        if cb is not None:
            cb(list(names))

    from gui_qt.note_editor_overlay import NoteEditorOverlay
    NoteEditorOverlay.show_over(view, title, initial, _save, _remove,
                                allow_remove=any(notes.get(nm) for nm in names))


def _remove_notes(view, names):
    """Remove the note from each mod without opening the editor."""
    pdir, notes = _profile_notes(view)
    if pdir is None or not names:
        return
    from Utils.profiles.state import write_mod_notes
    cur = dict(notes)
    removed = False
    for nm in names:
        if cur.pop(nm, None) is not None:
            removed = True
    if removed:
        try:
            write_mod_notes(pdir, cur)
        except Exception:
            pass
        cb = getattr(view, "on_notes_changed", None)
        if cb is not None:
            cb(list(names))


def _open_on_nexus_multi(view, names):
    """Open each selected mod's Nexus page (skips mods without one)."""
    try:
        from Utils.environment.xdg import open_url
    except Exception:
        return
    for nm in names:
        url = _mod_nexus_url(view, nm)
        if url:
            try:
                open_url(url)
            except Exception:
                pass


def _sort_selected_alphabetically(view, model, mod_rows):
    """Sort the SELECTED mods A→Z, writing them back into the same row slots the
    selection occupied (other rows + separators stay put). Port of Tk
    _sort_selected_alphabetically."""
    from gui_qt.modlist_model import _PINNED_NAMES
    sel = [model.entry(r) for r in mod_rows]
    sel = [e for e in sel
           if not e.is_separator and e.name not in _PINNED_NAMES]
    if len(sel) < 2:
        return
    # Mods with loose-file conflicts are left in their existing relative order
    # (sorting them could break a hand-tuned load order); they sink to the
    # bottom of the selection. Only conflict-free mods are sorted A→Z.
    conflicted = [e for e in sel if model.loose_conflict_code(e.name)]
    sortable = [e for e in sel if not model.loose_conflict_code(e.name)]
    key_fn = lambda e: e.display_name.casefold()
    if model.reverse_mode_active:
        sorted_entries = (list(reversed(conflicted))
                          + sorted(sortable, key=key_fn, reverse=True))
    else:
        sorted_entries = sorted(sortable, key=key_fn) + conflicted
    # Rebuild the body from the NATURAL order (the display may be a sorted
    # permutation); at each selected slot drop in the next sorted entry.
    # set_entries re-appends boundaries.
    sel_ids = {id(e) for e in sel}
    from Utils.diagnostics.conflicts import ConflictTimeline
    timing = ConflictTimeline("move", [e.name for e in sel])
    phase_started = timing.now()
    old_order = [e.name for e in model.natural_entries() if not e.is_separator]
    body: list = []
    it = iter(sorted_entries)
    for e in model.natural_entries():
        if e.name in _PINNED_NAMES:
            continue
        body.append(next(it) if id(e) in sel_ids else e)
    model.set_entries(body)
    # Pure (non-contiguous) reorder - _move_ctx handles per-mod crossings, so
    # a sort that flipped no conflicting pair skips the conflict rebuild.
    new_order = [e.name for e in model.natural_entries() if not e.is_separator]
    ctx = model._move_ctx(old_order, new_order, [e.name for e in sel])
    timing.mark("sort-selection Qt model update complete",
                phase_started=phase_started)
    try:
        save_ctx = (("move",) + ctx + (timing,) if ctx is not None
                    else ("full", timing))
        model.save(edit_ctx=save_ctx)
    except Exception as exc:
        timing.finish(f"sort-selection save failed: {exc}")


def _create_empty_mod(view, model, row):
    """Prompt for a name, create an empty staging folder + minimal meta.ini, and
    insert a new mod row just below *row*. Port of Tk _create_empty_mod."""
    _create_empty_mod_prompt(view, model,
                             lambda name: model.insert_mod(row, name, above=False))


def _create_empty_mod_at_boundary(view, model, top):
    """As _create_empty_mod, but insert at the top (below Overwrite) or bottom
    (below Root Folder) of the body - used from the boundary rows so it works
    even when the modlist is empty."""
    _create_empty_mod_prompt(view, model,
                             lambda name: model.insert_mod_at_body_edge(top, name))


def _create_empty_mod_prompt(view, model, insert):
    """Shared prompt + folder/meta.ini creation for the two create-empty-mod
    entry points. *insert* places the new row once the folder exists."""
    staging = getattr(view, "staging_dir", None)
    if staging is None:
        return

    def _named(name):
        name = (name or "").strip()
        if not name:
            return
        # Name-collision guard (mods + separators, by display name).
        existing = set()
        for r in range(model.rowCount()):
            e = model.entry(r)
            existing.add(e.name)
            existing.add(e.display_name)
        if name in existing:
            ConfirmOverlay.show_message(
                view, "Name conflict",
                f"A mod or separator named '{name}' already exists.")
            return
        try:
            from datetime import datetime
            mod_dir = staging / name
            created = not mod_dir.exists()
            mod_dir.mkdir(parents=True, exist_ok=True)
            installed = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
            (mod_dir / "meta.ini").write_text(
                f"[General]\ninstalled={installed}\n", encoding="utf-8")
        except OSError as exc:
            ConfirmOverlay.show_message(
                view, _mt("Create empty mod"),
                _mtf("Could not create the mod folder:\n{0}", exc))
            return
        if insert(name) is False and created:
            import shutil
            try:
                shutil.rmtree(mod_dir)
            except OSError:
                pass

    TextInputOverlay.show_over(view, _mt("Create empty mod"), _mt("Mod name:"),
                               _named, ok_label=_mt("Create"))


def _manage_overwrite(view):
    """Open the Overwrite manager tab (tree of the Overwrite folder, with
    move-to-mod / move-to-new-mod / delete). The window installs the callback in
    _reload_modlist; no-op if it isn't wired (e.g. headless)."""
    cb = getattr(view, "on_manage_overwrite", None)
    if callable(cb):
        cb()


def _show_overwrite_log(view, boundary_name=None):
    """Show the read-only restore-log overlay - files swept into the deploy
    target on restore, parsed from OVERWRITE_LOG_NAME. Overwrite reads
    game.get_effective_overwrite_path(); Root Folder reads
    game.get_effective_root_folder_path() (standard-deployed games sweep
    runtime files there, so it gets its own .mm_overwrite_log.txt)."""
    game = getattr(view, "game", None)
    if game is None:
        return
    from Utils.filegraph.constants import ROOT_FOLDER_NAME
    is_root = boundary_name == ROOT_FOLDER_NAME
    text = ""
    try:
        from Utils.deployment.shared import OVERWRITE_LOG_NAME
        base = (game.get_effective_root_folder_path() if is_root
                else game.get_effective_overwrite_path())
        log_path = base / OVERWRITE_LOG_NAME
        if log_path.is_file():
            text = log_path.read_text(encoding="utf-8")
    except Exception:
        text = ""
    title = (view.tr("Files swept into Root Folder (newest restore first)")
             if is_root
             else view.tr("Files swept into Overwrite (newest restore first)"))
    from gui_qt.overwrite_log_overlay import OverwriteLogOverlay, parse_overwrite_log
    OverwriteLogOverlay.show_over(view, parse_overwrite_log(text), title=title)


def _toggle_sep_lock(view, model, row):
    view._toggle_lock_row(row)


def _rename(view, model, row):
    e = model.entry(row)

    def _named(new):
        if new is None or not new.strip() or new.strip() == e.display_name:
            return
        if e.is_separator:
            # No folder on disk - a pure modlist.txt edit is the whole rename.
            # Migrate the separator's colour + deploy override to the new name
            # so they follow it (Tk parity), then persist via the window
            # callback.
            old_name = e.name
            model.rename(row, new.strip())
            new_name = model.entry(row).name
            cb = getattr(view, "on_separator_renamed", None)
            if callable(cb) and old_name != new_name:
                cb(old_name, new_name)
            return
        # Mods must go through the window: staging folder rename + modindex +
        # per-mod state migration (strip prefixes / disabled plugins / excluded
        # files / notes), not just the modlist.txt line.
        cb = getattr(view, "on_rename_mod", None)
        if callable(cb):
            cb(e.name, new.strip())

    # Separators have no folder (and so no meta.ini) - only mods get the
    # suggested-name dropdown.
    suggestions = [] if e.is_separator else _name_suggestions(view, e.name)
    TextInputOverlay.show_over(view, _mt("Rename"), _mt("New name:"), _named,
                               initial=e.display_name, ok_label=_mt("Rename"),
                               suggestions=suggestions)


def _name_suggestions(view, name):
    """Rename candidates for a staged mod (Nexus name, sibling version, …)."""
    staging = getattr(view, "staging_dir", None)
    if staging is None:
        return []
    try:
        from Utils.mods.names import suggest_names_for_staged_mod
        return suggest_names_for_staged_mod(staging, name)
    except Exception as exc:
        print(f"[gui_qt] name suggestions failed for {name!r}: {exc}", flush=True)
        return []


def _set_priority(view, model, row):
    cur = model.data(model.index(row, COL_NAME), 0)

    def _picked(text):
        try:
            val = int((text or "").strip())
        except ValueError:
            return
        model.set_priority(row, max(0, min(99999, val)))

    from PySide6.QtGui import QIntValidator
    TextInputOverlay.show_over(view, _mt("Set priority"),
                               _mtf("Priority for {0}:", cur),
                               _picked, initial="0",
                               validator=QIntValidator(0, 99999))


def _add_separator(view, model, row, above):
    def _named(name):
        if name and name.strip():
            model.add_separator(row, name.strip(), above)

    TextInputOverlay.show_over(view, _mt("Add separator"),
                               _mt("Separator name:"), _named, ok_label=_mt("Add"))


def _notify_mods_removed(view):
    """Tell the window a mod (and possibly its plugins) was removed, so the
    plugin panel can reload. No-op if the callback isn't wired."""
    cb = getattr(view, "on_mods_removed", None)
    if callable(cb):
        try:
            cb()
        except Exception as exc:
            print(f"[gui_qt] on_mods_removed failed: {exc}", flush=True)


def _group_owners(view, names):
    """{mod_name: member} when the active profile is a Profile Group, else
    None. Used to route removal member-side and name owners in the confirm."""
    profile_dir = getattr(view, "profile_dir", None)
    if profile_dir is None:
        return None
    try:
        from Utils.profiles.groups import is_group, owner_of
        if not is_group(profile_dir):
            return None
        owners = {}
        for n in names:
            o = owner_of(profile_dir, n)
            owners[n] = o[0] if o else None
        return owners
    except Exception:
        return None


def _locked_group_mods(view, names):
    """{mod_name: locked member} for group entries owned by a locked profile
    (removal must be refused), else {}."""
    profile_dir = getattr(view, "profile_dir", None)
    if profile_dir is None:
        return {}
    try:
        from Utils.profiles.groups import is_group, locked_owners
        if not is_group(profile_dir):
            return {}
        return locked_owners(profile_dir, list(names))
    except Exception:
        return {}


def _notify(view, text, state="warning"):
    win = getattr(view, "window", lambda: None)()
    cb = getattr(win, "_notify", None)
    if callable(cb):
        cb(text, state)
    else:
        print(f"[gui_qt] {text}", flush=True)


def _run_remove(view, game, profile_dir, names, owners) -> list:
    """Dispatch the file-side removal: group-aware when *owners* is set.
    Returns the names actually removed (a group skips locked members')."""
    log = lambda m: print(f"[remove] {m}", flush=True)  # noqa: E731
    try:
        if owners is not None:
            from Utils.profiles.groups import remove_mods_from_group
            return remove_mods_from_group(game, profile_dir, names, log_fn=log)
        from Utils.mods.remove import remove_mods
        remove_mods(game, profile_dir, names, log_fn=log)
        return list(names)
    except Exception as exc:
        print(f"[gui_qt] mod removal failed: {exc}", flush=True)
        return []


def _remove(view, model, row):
    """Fully remove a mod: undeploy its files, delete its staging folder, drop
    its index/BSA/plugins entries, then remove the modlist row. (Not just the
    list line - that left the files on disk so the mod still read as installed.)
    On a Profile Group the mod is removed from the OWNING MEMBER profile too -
    the confirm names it."""
    e = model.entry(row)
    if e is None or e.is_separator:
        return
    # A mod belonging to a LOCKED member profile can't be removed through the
    # group (the lock protects that profile's mods); it can still be removed
    # from the member profile itself.
    locked = _locked_group_mods(view, [e.name])
    if locked:
        _notify(view, _mt("'{0}' belongs to the locked profile '{1}' - switch "
                          "to that profile to remove it, or unlock it.")
                .format(e.display_name, locked[e.name]))
        return
    owners = _group_owners(view, [e.name])

    def _confirmed(ok):
        if not ok:
            return
        name = e.name
        game = getattr(view, "game", None)
        profile_dir = getattr(view, "profile_dir", None)
        removed = [name]
        if game is not None and profile_dir is not None:
            removed = _run_remove(view, game, profile_dir, [name], owners)
        if removed:
            model.remove_row(row)
        _notify_mods_removed(view)

    if owners is not None:
        owner = owners.get(e.name)
        where = (f"the member profile '{owner}' and this group" if owner
                 else "this group")
        msg = (f"Remove '{e.display_name}'?\n\nThis is a profile group - the "
               f"mod is removed from {where}, deleting its folder. This "
               f"cannot be undone.")
    else:
        msg = (f"Remove '{e.display_name}'?\n\nThis deletes the mod folder "
               "and cannot be undone.")
    ConfirmOverlay.show_over(view, "Remove mod", msg, _confirmed)


def _open_sep_settings(view, model, row):
    """Open the Separator Settings tab (colour + deploy override) for this
    separator. Reads current values keyed by the internal `..._separator` name
    (Tk storage key) and hands off to the window via the on_separator_settings
    callback."""
    e = model.entry(row)
    if e is None or not e.is_separator:
        return
    cb = getattr(view, "on_separator_settings", None)
    if not callable(cb):
        return
    current_color = model.sep_color(e.name)
    current_deploy = {}
    profile_dir = getattr(view, "profile_dir", None)
    if profile_dir is not None:
        try:
            from Utils.profiles.state import read_separator_deploy_paths
            current_deploy = read_separator_deploy_paths(profile_dir).get(
                e.name, {})
        except Exception:
            current_deploy = {}
    cb(e.name, current_color, current_deploy)


# ---- new wired handlers (separator remove / multi, mod multi-remove) -------

def _remove_separator(view, model, row):
    e = model.entry(row)
    if e is None or not e.is_separator:
        return

    def _confirmed(ok):
        if not ok:
            return
        removed = e.name
        model.remove_row(row)
        cb = getattr(view, "on_separators_removed", None)
        if callable(cb):
            cb([removed])

    ConfirmOverlay.show_over(view, "Remove separator",
                             f"Remove separator '{e.display_name}'?",
                             _confirmed)


def _remove_separators_multi(view, model, sep_rows):
    if not sep_rows:
        return

    def _confirmed(ok):
        if not ok:
            return
        # Remove high→low so earlier removals don't shift later row indices.
        removed = []
        for r in sorted(sep_rows, reverse=True):
            e = model.entry(r)
            if e is not None and e.is_separator:
                removed.append(e.name)
                model.remove_row(r, save=False)
        if removed:
            model.save()  # single save for the whole batch
        cb = getattr(view, "on_separators_removed", None)
        if callable(cb) and removed:
            cb(removed)

    ConfirmOverlay.show_over(view, "Remove separators",
                             f"Remove {len(sep_rows)} separator(s)?",
                             _confirmed)


def _set_sep_locks_multi(view, model, sep_rows, lock):
    """Lock/unlock every selected separator to *lock*, then save once."""
    changed = False
    for r in sep_rows:
        e = model.entry(r)
        if e is None or not e.is_separator:
            continue
        if model.is_sep_locked(e.display_name) != lock:
            model.toggle_sep_lock(r)
            changed = True
    if changed:
        view._save_separator_state()
        view.viewport().update()


def _remove_mods_multi(view, model, mod_rows):
    """Fully remove every selected mod (one confirm), then drop the rows.
    On a Profile Group each mod is removed from its owning member too."""
    rows = [r for r in mod_rows
            if (e := model.entry(r)) is not None
            and not e.is_separator and not e.locked]
    if not rows:
        return
    # Drop mods owned by a LOCKED member profile - they stay removable from
    # that profile itself, just not through the group.
    locked = _locked_group_mods(view, [model.entry(r).name for r in rows])
    if locked:
        rows = [r for r in rows if model.entry(r).name not in locked]
        _notify(view, _mt("{0} mod(s) skipped - they belong to locked "
                          "profile(s): {1}.")
                .format(len(locked), ", ".join(sorted(set(locked.values())))))
        if not rows:
            return
    names = [model.entry(r).name for r in rows]
    owners = _group_owners(view, names)

    def _confirmed(ok):
        if not ok:
            return
        game = getattr(view, "game", None)
        profile_dir = getattr(view, "profile_dir", None)
        removed = set(names)
        if game is not None and profile_dir is not None:
            removed = set(_run_remove(view, game, profile_dir, names, owners))
        gone = [r for r in rows if model.entry(r).name in removed]
        for r in sorted(gone, reverse=True):
            model.remove_row(r, save=False)
        if gone:
            model.save()  # single save → one filemap rebuild for the batch
        _notify_mods_removed(view)

    if owners is not None:
        members = sorted({m for m in owners.values() if m})
        msg = (f"Remove {len(names)} mod(s)?\n\nThis is a profile group - "
               f"the mods are removed from their member profile(s) "
               f"({', '.join(members) if members else 'none found'}) and "
               "this group, deleting their folders. This cannot be undone.")
    else:
        msg = (f"Remove {len(names)} mod(s)?\n\nThis deletes their folders "
               "and cannot be undone.")
    ConfirmOverlay.show_over(view, "Remove mods", msg, _confirmed)


# lupdate extraction anchors: every _mt/_mtf label above is translated at
# runtime via QCoreApplication.translate("ModListMenu", …), which lupdate
# cannot see through - so each literal is registered here explicitly.
_TR_MARKERS = (
    QT_TRANSLATE_NOOP("ModListMenu", "Abstain from Endorsement"),
    QT_TRANSLATE_NOOP("ModListMenu", "Abstain selected ({0})"),
    QT_TRANSLATE_NOOP("ModListMenu", "Add note"),
    QT_TRANSLATE_NOOP("ModListMenu", "Add note ({0})"),
    QT_TRANSLATE_NOOP("ModListMenu", "Add"),
    QT_TRANSLATE_NOOP("ModListMenu", "Add separator"),
    QT_TRANSLATE_NOOP("ModListMenu", "Add separator above"),
    QT_TRANSLATE_NOOP("ModListMenu", "Add separator below"),
    QT_TRANSLATE_NOOP("ModListMenu", "Bundle options…"),
    QT_TRANSLATE_NOOP("ModListMenu", "Change Version"),
    QT_TRANSLATE_NOOP("ModListMenu", "Check Updates"),
    QT_TRANSLATE_NOOP("ModListMenu", "Check Updates ({0})"),
    QT_TRANSLATE_NOOP("ModListMenu", "Clear Conflict Filter"),
    QT_TRANSLATE_NOOP("ModListMenu", "Copy to profile"),
    QT_TRANSLATE_NOOP("ModListMenu", "Copy to profile ({0})"),
    QT_TRANSLATE_NOOP("ModListMenu", "Could not create the mod folder:\n{0}"),
    QT_TRANSLATE_NOOP("ModListMenu", "Create"),
    QT_TRANSLATE_NOOP("ModListMenu", "Create an empty mod below"),
    QT_TRANSLATE_NOOP("ModListMenu", "Create empty mod"),
    QT_TRANSLATE_NOOP("ModListMenu", "Create empty mod below"),
    QT_TRANSLATE_NOOP("ModListMenu", "Disable Root Folder install"),
    QT_TRANSLATE_NOOP("ModListMenu", "Disable Root Folder install ({0})"),
    QT_TRANSLATE_NOOP("ModListMenu", "Disable selected ({0})"),
    QT_TRANSLATE_NOOP("ModListMenu", "Edit note"),
    QT_TRANSLATE_NOOP("ModListMenu", "Enable Root Folder install"),
    QT_TRANSLATE_NOOP("ModListMenu", "Enable Root Folder install ({0})"),
    QT_TRANSLATE_NOOP("ModListMenu", "Enable selected ({0})"),
    QT_TRANSLATE_NOOP("ModListMenu", "Endorse Mod"),
    QT_TRANSLATE_NOOP("ModListMenu", "Endorse selected ({0})"),
    QT_TRANSLATE_NOOP("ModListMenu", "Filter Conflicts"),
    QT_TRANSLATE_NOOP("ModListMenu", "'{0}' belongs to the locked profile "
                      "'{1}' - switch to that profile to remove it, or "
                      "unlock it."),
    QT_TRANSLATE_NOOP("ModListMenu", "{0} mod(s) skipped - they belong to "
                      "locked profile(s): {1}."),
    QT_TRANSLATE_NOOP("ModListMenu", "Lock Separator"),
    QT_TRANSLATE_NOOP("ModListMenu", "Lock Separators"),
    QT_TRANSLATE_NOOP("ModListMenu", "Log"),
    QT_TRANSLATE_NOOP("ModListMenu", "Manage Overwrite…"),
    QT_TRANSLATE_NOOP("ModListMenu", "Missing Requirements"),
    QT_TRANSLATE_NOOP("ModListMenu", "Missing Requirements ({0})"),
    QT_TRANSLATE_NOOP("ModListMenu", "View Requirements"),
    QT_TRANSLATE_NOOP("ModListMenu", "Mod name:"),
    QT_TRANSLATE_NOOP("ModListMenu", "Move to profile"),
    QT_TRANSLATE_NOOP("ModListMenu", "Move to profile ({0})"),
    QT_TRANSLATE_NOOP("ModListMenu", "Move to separator"),
    QT_TRANSLATE_NOOP("ModListMenu", "Move to separator ({0})"),
    QT_TRANSLATE_NOOP("ModListMenu", "Nexus Actions"),
    QT_TRANSLATE_NOOP("ModListMenu", "New name:"),
    QT_TRANSLATE_NOOP("ModListMenu", "Open folder"),
    QT_TRANSLATE_NOOP("ModListMenu", "Open in NIF Viewer"),
    QT_TRANSLATE_NOOP("ModListMenu", "Open on Nexus"),
    QT_TRANSLATE_NOOP("ModListMenu", "Open on Nexus ({0})"),
    QT_TRANSLATE_NOOP("ModListMenu", "Open on mod.io"),
    QT_TRANSLATE_NOOP("ModListMenu", "Open on Thunderstore"),
    QT_TRANSLATE_NOOP("ModListMenu", "Thunderstore Actions"),
    QT_TRANSLATE_NOOP("ModListMenu", "Quick Update"),
    QT_TRANSLATE_NOOP("ModListMenu", "Quick Update ({0})"),
    QT_TRANSLATE_NOOP("ModListMenu", "Reinstall ({0})"),
    QT_TRANSLATE_NOOP("ModListMenu", "Reinstall (Redownload)"),
    QT_TRANSLATE_NOOP("ModListMenu", "Reinstall Mod"),
    QT_TRANSLATE_NOOP("ModListMenu", "Remove mod"),
    QT_TRANSLATE_NOOP("ModListMenu", "Remove mod ({0})"),
    QT_TRANSLATE_NOOP("ModListMenu", "Remove note ({0})"),
    QT_TRANSLATE_NOOP("ModListMenu", "Remove separator"),
    QT_TRANSLATE_NOOP("ModListMenu", "Remove separators ({0})"),
    QT_TRANSLATE_NOOP("ModListMenu", "Rename"),
    QT_TRANSLATE_NOOP("ModListMenu", "Rename mod"),
    QT_TRANSLATE_NOOP("ModListMenu", "Rename separator"),
    QT_TRANSLATE_NOOP("ModListMenu", "Search…"),
    QT_TRANSLATE_NOOP("ModListMenu", "Separator name:"),
    QT_TRANSLATE_NOOP("ModListMenu", "Separator settings…"),
    QT_TRANSLATE_NOOP("ModListMenu", "Set priority"),
    QT_TRANSLATE_NOOP("ModListMenu", "Set priority…"),
    QT_TRANSLATE_NOOP("ModListMenu", "Priority for {0}:"),
    QT_TRANSLATE_NOOP("ModListMenu", "Show Conflicts"),
    QT_TRANSLATE_NOOP("ModListMenu", "Sort Alphabetically ({0})"),
    QT_TRANSLATE_NOOP("ModListMenu", "Track Mod"),
    QT_TRANSLATE_NOOP("ModListMenu", "Track Mod ({0})"),
    QT_TRANSLATE_NOOP("ModListMenu", "Unlock Separator"),
    QT_TRANSLATE_NOOP("ModListMenu", "Unlock Separators"),
    QT_TRANSLATE_NOOP("ModListMenu", "{0} ({1})"),
)
