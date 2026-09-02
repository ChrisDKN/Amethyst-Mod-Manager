"""Right-click context menu for the Plugins panel.

Mirrors the Tk menu (gui/plugin_panel.py `_show_plugin_context_menu`, 4760-4935)
and follows the same show-vs-hide convention as the modlist menu
(gui_qt/modlist_menu.py): each item is SHOWN only when its Tk condition holds and
HIDDEN otherwise. The only greyed items are the ones still awaiting a Qt backend
(BOS-SP / overlapping-plugins / LOOT location links), and even those appear only when
their Tk show-condition passes.

Vanilla (base-game) plugins are always-on and can't be toggled - right-clicking a
vanilla-only selection shows NO menu (Tk parity: it filters to non-vanilla rows and
returns early if none remain).

Core items wired: Enable / Disable (single + multi), the ESL flag toggle
(single + multi), and the userlist items (Add to userlist / Add to group /
Remove from userlist / Show cycle / Show userlist rules - via view callbacks
set by app._reload_plugins), plus links embedded in LOOT messages. The rest are
gated greyed stubs.
"""

from __future__ import annotations

import re
from urllib.parse import urlsplit

from PySide6.QtWidgets import QMenu
from PySide6.QtGui import QAction
from PySide6.QtCore import Qt, QCoreApplication, QT_TRANSLATE_NOOP


_MARKDOWN_LINK_RE = re.compile(
    r"\[([^\]]+)\]\(\s*(https?://[^\s)]+)\s*\)", re.IGNORECASE)
_WEB_URL_RE = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)


def _mt(label: str) -> str:
    """Translate a plugin context-menu label (module-level functions have no
    `self`). Literals registered for lupdate in _TR_MARKERS at file end."""
    return QCoreApplication.translate("PluginMenu", label)


def _mtf(template: str, *args) -> str:
    """_mt for count-labels: translate the {0}-template then format."""
    return QCoreApplication.translate("PluginMenu", template).format(*args)



def show_context_menu(view, global_pos, index):
    """Build + exec the plugins context menu for *index* at *global_pos*."""
    menu = build_context_menu(view, index)
    if menu is not None:
        menu.exec(global_pos)


def build_context_menu(view, index):
    """Construct (but don't exec) the context QMenu - split out so headless tests
    can inspect the actions. Returns None if there's no menu (e.g. vanilla-only)."""
    model = view.model()
    if not index.isValid():
        return None

    # Selected rows, filtered to non-vanilla ("toggleable") - Tk hides the whole
    # menu when nothing toggleable is selected.
    sel_rows = sorted({i.row() for i in view.selectionModel().selectedRows()
                       or view.selectionModel().selectedIndexes()})
    if not sel_rows:
        sel_rows = [index.row()]
    toggleable = [r for r in sel_rows
                  if 0 <= r < model.rowCount() and not model.row(r).vanilla]
    if not toggleable:
        return None
    multi = len(toggleable) > 1

    menu = QMenu(view)
    state = {"group_started": False, "any": False}

    def _connect(action, slot):
        # QAction.triggered emits a `checked` bool. If a slot captures data via a
        # default arg (e.g. `lambda ns=idxs:`), Qt passes `checked` positionally
        # and clobbers that default. Wrap so the bool is always swallowed.
        action.triggered.connect(lambda _checked=False, _s=slot: _s())

    def act(label, slot, enabled=True):
        a = QAction(label, menu)
        _connect(a, slot)
        a.setEnabled(enabled)
        menu.addAction(a)
        state["group_started"] = True
        state["any"] = True
        return a

    def stub(label):
        # Greyed-out placeholder for an action not yet wired.
        return act(label, lambda: None, enabled=False)

    def submenu(label, items, enabled=True):
        sub = QMenu(label, menu)
        sub.setEnabled(enabled)
        for text, slot in items:
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

    _build_plugin_menu(view, model, index.row(), toggleable, multi,
                       act, stub, submenu, divider)
    return menu if state["any"] else None


def _build_plugin_menu(view, model, row, toggleable, multi,
                       act, stub, submenu, divider):
    game = getattr(view, "game", None)

    # ---- Enable / Disable (always) ---------------------------------------
    if multi:
        n = len(toggleable)
        act(_mtf("Enable selected ({0})", n),
            lambda: _set_enabled(view, toggleable, True))
        act(_mtf("Disable selected ({0})", n),
            lambda: _set_enabled(view, toggleable, False))
    else:
        act(_mt("Enable plugin"), lambda: _set_enabled(view, toggleable, True))
        act(_mt("Disable plugin"), lambda: _set_enabled(view, toggleable, False))

    # ---- OpenMW groundcover classification -------------------------------
    groundcover_exts = tuple(
        ext.lower() for ext in
        (getattr(game, "groundcover_plugin_extensions", ()) or ())
    )
    groundcover_rows = []
    if groundcover_exts:
        from gui_qt.plugin_state import PF_GROUNDCOVER
        groundcover_rows = [
            i for i in toggleable
            if (model.row(i).name.lower().endswith(groundcover_exts)
                or model.row(i).flags & PF_GROUNDCOVER)
        ]
    if groundcover_rows:
        marked = [i for i in groundcover_rows
                  if model.row(i).flags & PF_GROUNDCOVER]
        normal = [i for i in groundcover_rows
                  if not model.row(i).flags & PF_GROUNDCOVER]
        divider()
        if multi:
            if normal:
                act(_mtf("Use selected as OpenMW groundcover ({0})", len(normal)),
                    lambda rows=normal: _set_groundcover(view, rows, True))
            if marked:
                act(_mtf("Use selected as normal OpenMW content ({0})", len(marked)),
                    lambda rows=marked: _set_groundcover(view, rows, False))
        elif marked:
            act(_mt("Use as normal OpenMW content"),
                lambda: _set_groundcover(view, groundcover_rows, False))
        else:
            act(_mt("Use as OpenMW groundcover"),
                lambda: _set_groundcover(view, groundcover_rows, True))

    # ---- Disable - BOS/SkyPatcher patch replaces it (stub) ----------------
    # Tk: gated on _bos_sp_plugins detection. Qt has no BOS/SP backend yet, so
    # _bos_sp_kind()/_bos_sp_rows() return empty → hidden until that lands.
    if multi:
        bos_rows = _bos_sp_rows(view, toggleable)
        if bos_rows:
            stub(_mtf("Disable {0} BOS/SP-patched (safe to disable)",
                      len(bos_rows)))
    else:
        kind = _bos_sp_kind(view, model.row(row).name)
        if kind:
            label = {"bos": "BOS", "sp": "SkyPatcher",
                     "both": "BOS+SkyPatcher"}.get(kind, kind)
            stub(_mtf("Disable - {0} patch replaces it", label))

    # ---- ESL flag toggle --------------------------------------------------
    if getattr(game, "supports_esl_flag", False):
        # Only .esp/.esm rows can toggle (.esl is always light by extension).
        esl_rows = [i for i in toggleable
                    if not model.row(i).name.lower().endswith(".esl")]
        if esl_rows:
            divider()
            _build_esl_items(view, model, esl_rows, multi, act, stub)

    # ---- userlist / groups / cycles (LOOT userlist.yaml) -------------------
    # The app sets the callbacks + membership sets on the view in
    # _reload_plugins; hide the whole block when they're absent (no profile).
    divider()
    ul_add = getattr(view, "on_userlist_add", None)
    grp_add = getattr(view, "on_group_add", None)
    ul_remove = getattr(view, "on_userlist_remove", None)
    show_cycle = getattr(view, "on_show_cycle", None)
    if not multi:
        name = model.row(row).name
        if not _in_userlist(view, name) and callable(ul_add):
            act(_mt("Add to userlist…"),
                lambda n=name, r=row: ul_add(n, r))
        if callable(grp_add):
            act(_mt("Add to group…"), lambda n=name: grp_add([n]))
        if _in_userlist(view, name) and callable(ul_remove):
            act(_mt("Remove from userlist"), lambda n=name: ul_remove([n]))
        if _in_cycle(view, name) and callable(show_cycle):
            act(_mt("Show cycle…"), lambda n=name: show_cycle(n))
        elif _in_userlist(view, name) and callable(show_cycle):
            act(_mt("Show userlist rules…"), lambda n=name: show_cycle(n))
    else:
        names = [model.row(i).name for i in toggleable]
        if callable(grp_add):
            act(_mt("Add selected to group…"), lambda ns=names: grp_add(ns))
        if any(_in_userlist(view, n) for n in names) and callable(ul_remove):
            act(_mt("Remove selected from userlist"),
                lambda ns=names: ul_remove(ns))

    # ---- Show overlapping plugins… (gated on loot_sort_enabled) ----------
    # Record overlap = full libloot load; runs on a worker thread (app side).
    on_overlap = getattr(view, "on_show_overlapping", None)
    if not multi and getattr(game, "loot_sort_enabled", False):
        divider()
        name = model.row(row).name
        if callable(on_overlap):
            act(_mt("Show overlapping plugins…"),
                lambda n=name: on_overlap(n))
        else:
            stub(_mt("Show overlapping plugins…"))

    # ---- Links embedded in LOOT messages ---------------------------------
    # LOOT message text is Markdown. Only offer an action when the selected
    # plugin actually has an http(s) link; most LOOT messages are plain text.
    if not multi:
        links = _loot_message_links(model.row(row).loot_info)
        if links:
            divider()
            if len(links) == 1:
                act(_mt("Open LOOT message link"),
                    lambda u=links[0][1]: _open_url(u))
            else:
                submenu(
                    _mt("Open LOOT message link…"),
                    [(label, lambda u=url: _open_url(u))
                     for label, url in links],
                )

    # ---- LOOT masterlist location links (stub - _loot_info not in Qt) -----
    if not multi:
        for text in _loot_locations(view, model.row(row).name):
            stub(text)


def _build_esl_items(view, model, esl_rows, multi, act, stub):
    """ESL flag sub-items. Ports the Tk single/multi eligibility logic."""
    game = getattr(view, "game", None)
    game_type_attr = getattr(game, "loot_game_type", "") or ""
    paths = _plugin_paths(view)

    from Utils.plugins.parser import is_esl_flagged, check_esl_eligible

    def esl_state(i):
        p = paths.get(model.row(i).name.lower())
        flagged = bool(p and p.is_file() and is_esl_flagged(p))
        eligible = bool(p and p.is_file() and check_esl_eligible(p, game_type_attr))
        return p, flagged, eligible

    if not multi:
        i = esl_rows[0]
        p, flagged, eligible = esl_state(i)
        if flagged:
            act(_mt("Remove ESL flag (un-light)"),
                lambda: _toggle_esl(view, [i], False))
        elif eligible:
            act(_mt("Mark as Light (ESL)"),
                lambda: _toggle_esl(view, [i], True))
        else:
            # Present but greyed - matches Tk's disabled "not ESL-safe" entry.
            stub(_mt("Not ESL-safe (per LOOT - compact in xEdit first)"))
        return

    # Multi.
    not_esl, already_esl, ineligible = [], [], 0
    for i in esl_rows:
        _p, flagged, eligible = esl_state(i)
        if flagged:
            already_esl.append(i)
        elif eligible:
            not_esl.append(i)
        else:
            ineligible += 1
    if not_esl:
        suffix = (_mtf(" ({0} ineligible skipped)", ineligible)
                  if ineligible else "")
        act(_mtf("Mark selected as Light (ESL) ({0})", len(not_esl)) + suffix,
            lambda: _toggle_esl(view, not_esl, True))
    elif ineligible:
        stub(_mtf("Mark as Light (ESL) - none eligible "
                  "({0} need xEdit compact)", ineligible))
    if already_esl:
        act(_mtf("Remove ESL flag from selected ({0})", len(already_esl)),
            lambda: _toggle_esl(view, already_esl, False))


# ---- actions --------------------------------------------------------------
def _set_enabled(view, indices, enabled: bool):
    view.model().set_enabled(indices, enabled)
    cb = getattr(view, "on_plugins_changed", None)
    if callable(cb):
        cb()


def _set_groundcover(view, indices, enabled: bool):
    model = view.model()
    game = getattr(view, "game", None)
    profile_dir = getattr(view, "profile_dir", None)
    if game is None or profile_dir is None:
        return
    names = [
        model.row(i).name for i in indices
        if 0 <= i < model.rowCount()
    ]
    if not names:
        return
    try:
        from gui_qt.plugin_state import (
            PF_GROUNDCOVER,
            groundcover_plugins_for_profile,
        )
        from Utils.profiles.state import write_groundcover_plugins
        current = {
            name.lower(): name
            for name in groundcover_plugins_for_profile(game, profile_dir)
        }
        for name in names:
            if enabled:
                current[name.lower()] = name
            else:
                current.pop(name.lower(), None)
        write_groundcover_plugins(profile_dir, current.values())
    except Exception as exc:
        model.save_failed.emit(_mtf("Groundcover setting save failed: {0}", exc))
        return

    changed = {name.lower() for name in names}
    for plugin in model.natural_rows():
        if plugin.name.lower() not in changed:
            continue
        if enabled:
            plugin.flags |= PF_GROUNDCOVER
        else:
            plugin.flags &= ~PF_GROUNDCOVER
    if model.rowCount():
        from gui_qt.plugin_model import COL_FLAGS, PFlagsRole
        model.dataChanged.emit(
            model.index(0, COL_FLAGS),
            model.index(model.rowCount() - 1, COL_FLAGS),
            [PFlagsRole, Qt.ToolTipRole],
        )
        model.flags_changed()


def _toggle_esl(view, indices, enable: bool):
    """Port of Tk _toggle_esl_flag: skip .esl / unknown-path / ineligible rows,
    write the header flag, then refresh so the flag column repaints."""
    from Utils.plugins.parser import set_esl_flag, check_esl_eligible
    model = view.model()
    game = getattr(view, "game", None)
    game_type_attr = getattr(game, "loot_game_type", "") or ""
    paths = _plugin_paths(view)
    changed = 0
    for i in indices:
        if not (0 <= i < model.rowCount()):
            continue
        name = model.row(i).name
        if name.lower().endswith(".esl"):
            continue
        p = paths.get(name.lower())
        if p is None or not p.is_file():
            continue
        if enable and not check_esl_eligible(p, game_type_attr):
            continue
        if set_esl_flag(p, enable):
            changed += 1
    if changed:
        cb = getattr(view, "on_plugins_changed", None)
        if callable(cb):
            cb()   # re-reads headers → ESL bit + stats + banner refresh


# ---- helpers / predicates -------------------------------------------------
def _plugin_paths(view) -> dict:
    """{plugin name (lower) → on-disk Path} for the active game (staging mod /
    overwrite / vanilla Data). Reuses the same resolver the Flags column uses."""
    game = getattr(view, "game", None)
    if game is None:
        return {}
    resolved = getattr(view, "resolved_paths", None)
    if resolved is not None:
        return resolved
    try:
        from gui_qt.plugin_state import resolve_plugin_paths_for_game
        return resolve_plugin_paths_for_game(game)
    except Exception:
        return {}


# The following predicates gate the greyed stubs. They return empty/false until
# their Tk backend is ported to Qt (userlist.yaml overlays, BOS/SP detection,
# LOOT masterlist cache). Wiring an item later = fill in the predicate + swap the
# stub() for act().
def _bos_sp_kind(view, name: str) -> str:
    return ""


def _bos_sp_rows(view, indices) -> list:
    return []


def _in_userlist(view, name: str) -> bool:
    """Plugin has an entry in userlist.yaml (set pushed by app._reload_plugins)."""
    return name.lower() in (getattr(view, "userlist_plugins", None) or set())


def _in_cycle(view, name: str) -> bool:
    """Plugin's userlist rules form a broken cycle (set pushed by the app)."""
    return name.lower() in (getattr(view, "userlist_cycles", None) or set())


def _trim_url(url: str) -> str:
    """Remove prose/Markdown punctuation captured after a bare web URL."""
    url = url.rstrip(".,;:!?")
    pairs = ((")", "("), ("]", "["), ("}", "{"))
    changed = True
    while changed and url:
        changed = False
        for close, opening in pairs:
            if url.endswith(close) and url.count(close) > url.count(opening):
                url = url[:-1]
                changed = True
    return url


def _valid_web_url(url: str) -> bool:
    try:
        parsed = urlsplit(url)
    except ValueError:
        return False
    return parsed.scheme.lower() in {"http", "https"} and bool(parsed.netloc)


def _loot_message_links(info: dict | None) -> list[tuple[str, str]]:
    """Return unique ``(label, URL)`` pairs from LOOT Markdown messages."""
    if not isinstance(info, dict):
        return []

    links: list[tuple[str, str]] = []
    seen: set[str] = set()

    def add(label: str, raw_url: str) -> None:
        url = _trim_url(raw_url.strip())
        if not _valid_web_url(url) or url in seen:
            return
        seen.add(url)
        clean_label = re.sub(r"[*_`]+", "", label).strip()
        if not clean_label:
            clean_label = urlsplit(url).netloc
        if len(clean_label) > 80:
            clean_label = clean_label[:77].rstrip() + "…"
        links.append((clean_label, url))

    for message in info.get("messages") or []:
        if not isinstance(message, dict):
            continue
        text = message.get("text", "")
        if not isinstance(text, str):
            continue
        markdown_spans = []
        for match in _MARKDOWN_LINK_RE.finditer(text):
            add(match.group(1), match.group(2))
            markdown_spans.append(match.span())
        # Also accept plain and angle-bracketed URLs. Markdown URLs found above
        # are skipped here so surrounding emphasis markers cannot become part
        # of a second, malformed URL (for example ``[link](url)**``).
        for match in _WEB_URL_RE.finditer(text):
            if any(start <= match.start() < end
                   for start, end in markdown_spans):
                continue
            add("", match.group(0))
    return links


def _open_url(url: str) -> None:
    """Open a validated LOOT link using the host/Flatpak-aware launcher."""
    if not _valid_web_url(url):
        return
    from Utils.environment.xdg import open_url
    open_url(url)


def _loot_locations(view, name: str) -> list:
    return []


# lupdate extraction anchors - every _mt/_mtf label above, translated at
# runtime via QCoreApplication.translate("PluginMenu", …) which lupdate
# cannot see through.
_TR_MARKERS = (
    QT_TRANSLATE_NOOP("PluginMenu", " ({0} ineligible skipped)"),
    QT_TRANSLATE_NOOP("PluginMenu", "Add selected to group…"),
    QT_TRANSLATE_NOOP("PluginMenu", "Add to group…"),
    QT_TRANSLATE_NOOP("PluginMenu", "Add to userlist…"),
    QT_TRANSLATE_NOOP("PluginMenu", "Disable plugin"),
    QT_TRANSLATE_NOOP("PluginMenu", "Disable selected ({0})"),
    QT_TRANSLATE_NOOP("PluginMenu", "Disable {0} BOS/SP-patched (safe to disable)"),
    QT_TRANSLATE_NOOP("PluginMenu", "Disable - {0} patch replaces it"),
    QT_TRANSLATE_NOOP("PluginMenu", "Enable plugin"),
    QT_TRANSLATE_NOOP("PluginMenu", "Enable selected ({0})"),
    QT_TRANSLATE_NOOP("PluginMenu", "Groundcover setting save failed: {0}"),
    QT_TRANSLATE_NOOP("PluginMenu", "Mark as Light (ESL)"),
    QT_TRANSLATE_NOOP("PluginMenu", "Mark as Light (ESL) - none eligible "),
    QT_TRANSLATE_NOOP("PluginMenu", "Mark selected as Light (ESL) ({0})"),
    QT_TRANSLATE_NOOP("PluginMenu", "Not ESL-safe (per LOOT - compact in xEdit first)"),
    QT_TRANSLATE_NOOP("PluginMenu", "Open LOOT message link"),
    QT_TRANSLATE_NOOP("PluginMenu", "Open LOOT message link…"),
    QT_TRANSLATE_NOOP("PluginMenu", "Remove ESL flag (un-light)"),
    QT_TRANSLATE_NOOP("PluginMenu", "Remove ESL flag from selected ({0})"),
    QT_TRANSLATE_NOOP("PluginMenu", "Remove from userlist"),
    QT_TRANSLATE_NOOP("PluginMenu", "Remove selected from userlist"),
    QT_TRANSLATE_NOOP("PluginMenu", "Show cycle…"),
    QT_TRANSLATE_NOOP("PluginMenu", "Show overlapping plugins…"),
    QT_TRANSLATE_NOOP("PluginMenu", "Show userlist rules…"),
    QT_TRANSLATE_NOOP("PluginMenu", "Use as normal OpenMW content"),
    QT_TRANSLATE_NOOP("PluginMenu", "Use as OpenMW groundcover"),
    QT_TRANSLATE_NOOP("PluginMenu", "Use selected as normal OpenMW content ({0})"),
    QT_TRANSLATE_NOOP("PluginMenu", "Use selected as OpenMW groundcover ({0})"),
)
