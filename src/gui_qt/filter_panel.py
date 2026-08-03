"""Shared filter side panel for the Qt UI.

A single reusable widget every panel (modlist, plugins, data) docks on its left
edge — mirroring the Tk inline filter side panel. It's driven by a *spec* so each
host supplies its own sections/checkboxes; the panel emits a flat state dict
(key -> tri-state int 0/1/2, plus the dynamic category/filetype include/exclude
frozensets) whenever anything changes.

Spec shape (list of sections, in display order):

    [
        {"title": "By status", "type": "checks",
         "items": [(key, label, enabled_bool), ...]},
        {"title": "By category", "type": "dynamic", "id": "categories"},
        {"title": "By file type", "type": "dynamic", "id": "filetypes"},
    ]

Dynamic sections are repopulated by the host via set_dynamic_items(id, items)
where items is a list of (key, label, count|None). Their selected/excluded keys
come back in the state dict as "<id>" (include frozenset) and "<id>_exclude".
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal, QT_TRANSLATE_NOOP
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QScrollArea, QFrame,
    QPushButton, QSizePolicy,
)

from gui_qt.tri_state_checkbox import TriStateCheckBox, STATE_OFF
from gui_qt.theme_qt import active_palette, _c

PANEL_WIDTH = 320

# Filter labels + section titles are defined in the spec-supplying modules
# (modlist_filter.STATUS_FILTERS / PLUGIN_STATUS_FILTERS, mod_files_view,
# downloads_view, app.py) as canonical English paired with persistence keys.
# The panel translates them at display time via self.tr(...) under THIS
# (FilterSidePanel) context; lupdate can't see them there (they arrive as
# variables), so every label/title is registered here with a direct literal
# QT_TRANSLATE_NOOP call. Keep in sync when a filter is added.
_TR_MARKERS = (
    # --- section titles ---
    QT_TRANSLATE_NOOP("FilterSidePanel", "By status"),
    QT_TRANSLATE_NOOP("FilterSidePanel", "By category"),
    QT_TRANSLATE_NOOP("FilterSidePanel", "By author"),
    QT_TRANSLATE_NOOP("FilterSidePanel", "By file type"),
    QT_TRANSLATE_NOOP("FilterSidePanel", "By conflict"),
    QT_TRANSLATE_NOOP("FilterSidePanel", "By location"),
    QT_TRANSLATE_NOOP("FilterSidePanel", "By source"),
    QT_TRANSLATE_NOOP("FilterSidePanel", "Filters"),
    # --- modlist STATUS_FILTERS ---
    QT_TRANSLATE_NOOP("FilterSidePanel", "Disabled mods"),
    QT_TRANSLATE_NOOP("FilterSidePanel", "Enabled mods"),
    QT_TRANSLATE_NOOP("FilterSidePanel", "Hide separators"),
    QT_TRANSLATE_NOOP("FilterSidePanel", "Winning conflicts"),
    QT_TRANSLATE_NOOP("FilterSidePanel", "Losing conflicts"),
    QT_TRANSLATE_NOOP("FilterSidePanel", "Winning & losing conflicts"),
    QT_TRANSLATE_NOOP("FilterSidePanel", "Fully conflicted mods"),
    QT_TRANSLATE_NOOP("FilterSidePanel", "Missing requirements"),
    QT_TRANSLATE_NOOP("FilterSidePanel", "Mods with disabled plugins"),
    QT_TRANSLATE_NOOP("FilterSidePanel", "Mods with plugins"),
    QT_TRANSLATE_NOOP("FilterSidePanel", "Mods modified in Mod Files tab"),
    QT_TRANSLATE_NOOP("FilterSidePanel", "Mods with updates"),
    QT_TRANSLATE_NOOP("FilterSidePanel", "Mods with notes"),
    QT_TRANSLATE_NOOP("FilterSidePanel", "FOMOD mods"),
    QT_TRANSLATE_NOOP("FilterSidePanel", "BAIN mods"),
    QT_TRANSLATE_NOOP("FilterSidePanel", "Mods with BSA archives"),
    QT_TRANSLATE_NOOP("FilterSidePanel", "Mods with BA2 archives"),
    QT_TRANSLATE_NOOP("FilterSidePanel", "PGPatcher mods"),
    # --- plugin PLUGIN_STATUS_FILTERS ---
    QT_TRANSLATE_NOOP("FilterSidePanel", "Enabled plugins"),
    QT_TRANSLATE_NOOP("FilterSidePanel", "Disabled plugins"),
    QT_TRANSLATE_NOOP("FilterSidePanel", "ESL-flagged (light)"),
    QT_TRANSLATE_NOOP("FilterSidePanel", "Not ESL-flagged"),
    QT_TRANSLATE_NOOP("FilterSidePanel", "Extension .esl"),
    QT_TRANSLATE_NOOP("FilterSidePanel", "Extension .esm"),
    QT_TRANSLATE_NOOP("FilterSidePanel", "Extension .esp"),
    QT_TRANSLATE_NOOP("FilterSidePanel", "Missing masters"),
    QT_TRANSLATE_NOOP("FilterSidePanel", "Dirty (needs cleaning)"),
    QT_TRANSLATE_NOOP("FilterSidePanel", "Managed by userlist"),
    QT_TRANSLATE_NOOP("FilterSidePanel", "Bash-tagged"),
    QT_TRANSLATE_NOOP("FilterSidePanel", "ESL-safe (eligible for ESL flag)"),
    QT_TRANSLATE_NOOP("FilterSidePanel", "ESL-unsafe (too many records)"),
    QT_TRANSLATE_NOOP("FilterSidePanel", "BOS/SP-patched"),
    QT_TRANSLATE_NOOP("FilterSidePanel", "BOS-patched"),
    QT_TRANSLATE_NOOP("FilterSidePanel", "SkyPatcher-patched"),
    # --- downloads / mod-files panels ---
    QT_TRANSLATE_NOOP("FilterSidePanel", "Show only installed"),
    QT_TRANSLATE_NOOP("FilterSidePanel", "Show only not installed"),
    # --- data panel ---
    QT_TRANSLATE_NOOP("FilterSidePanel", "Only conflicts"),
    # --- text files panel (source checks) ---
    QT_TRANSLATE_NOOP("FilterSidePanel", "Mod folders"),
    QT_TRANSLATE_NOOP("FilterSidePanel", "Profile"),
    QT_TRANSLATE_NOOP("FilterSidePanel", "Game folder"),
    QT_TRANSLATE_NOOP("FilterSidePanel", "My Games"),
)


class FilterSidePanel(QWidget):
    """Collapsible filter panel. `changed(dict)` fires on every edit; the dict
    is the full current state (every check key + dynamic include/exclude sets).
    """

    changed = Signal(dict)
    close_requested = Signal()

    def __init__(self, spec: list[dict], parent=None, *, title: str = "Filters"):
        super().__init__(parent)
        self._spec = spec
        self._checks: dict[str, TriStateCheckBox] = {}
        # Dynamic sections: id -> (container layout, {key: TriStateCheckBox})
        self._dynamic: dict[str, tuple] = {}
        self._dynamic_meta: dict[str, dict] = {}   # id -> section dict
        self.setObjectName("FilterPanel")
        self.setFixedWidth(PANEL_WIDTH)
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Expanding)
        self._build(title)

    # -- construction ---------------------------------------------------------
    def _build(self, title: str) -> None:
        p = active_palette()
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # Header: title | Clear all | ×
        header = QWidget()
        header.setObjectName("FilterHeader")
        hb = QHBoxLayout(header)
        hb.setContentsMargins(10, 6, 8, 6)
        hb.setSpacing(6)
        # Spec titles/labels arrive as canonical English (they double as/ pair
        # with persistence keys), so translate them at display time here.
        lbl = QLabel(self.tr(title))
        lbl.setObjectName("FilterTitle")
        hb.addWidget(lbl)
        hb.addStretch(1)
        clear = QPushButton(self.tr("Clear all"))
        clear.setObjectName("FilterClearBtn")
        clear.setCursor(Qt.PointingHandCursor)
        clear.clicked.connect(self.clear_all)
        hb.addWidget(clear)
        close = QPushButton("✕")
        close.setObjectName("FilterCloseBtn")
        close.setCursor(Qt.PointingHandCursor)
        close.setFixedWidth(26)
        close.clicked.connect(self.close_requested.emit)
        hb.addWidget(close)
        outer.addWidget(header)

        line = QFrame()
        line.setObjectName("FilterRule")
        line.setFixedHeight(1)
        outer.addWidget(line)

        # Scrollable body
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        body = QWidget()
        body.setObjectName("FilterBody")
        self._body_layout = QVBoxLayout(body)
        self._body_layout.setContentsMargins(10, 8, 10, 12)
        self._body_layout.setSpacing(3)

        for section in self._spec:
            self._add_section(section)

        self._body_layout.addStretch(1)
        scroll.setWidget(body)
        outer.addWidget(scroll, 1)

        self.setStyleSheet(self._qss(p))

    def _add_section(self, section: dict) -> None:
        _t = section.get("title", "")
        title = QLabel(self.tr(_t) if _t else "")
        title.setObjectName("FilterSectionTitle")
        self._body_layout.addSpacing(6)
        self._body_layout.addWidget(title)

        stype = section.get("type", "checks")
        if stype == "checks":
            for key, label, enabled in section.get("items", []):
                cb = TriStateCheckBox(self.tr(label))
                cb.setEnabled(bool(enabled))
                if not enabled:
                    cb.setToolTip(self.tr("Not available for this game / not yet wired"))
                cb.stateChanged.connect(self._emit)
                self._checks[key] = cb
                self._body_layout.addWidget(cb)
        elif stype == "dynamic":
            container = QWidget()
            clay = QVBoxLayout(container)
            clay.setContentsMargins(0, 0, 0, 0)
            clay.setSpacing(2)
            self._body_layout.addWidget(container)
            self._dynamic[section["id"]] = (clay, {})
            self._dynamic_meta[section["id"]] = section
            # Placeholder until populated.
            self._set_dynamic_placeholder(section["id"], self.tr("(none)"))

    def _set_dynamic_placeholder(self, sec_id: str, text: str) -> None:
        clay, _checks = self._dynamic[sec_id]
        ph = QLabel(text)
        ph.setObjectName("FilterEmpty")
        clay.addWidget(ph)

    # -- dynamic sections -----------------------------------------------------
    def set_dynamic_items(self, sec_id: str,
                          items: list[tuple]) -> None:
        """Repopulate a dynamic section. items = [(key, label, count|None), ...].
        Preserves any existing tri-state for keys that survive."""
        if sec_id not in self._dynamic:
            return
        clay, checks = self._dynamic[sec_id]
        prev = {k: cb.state() for k, cb in checks.items()}
        # Clear
        while clay.count():
            it = clay.takeAt(0)
            w = it.widget()
            if w is not None:
                w.deleteLater()
        checks.clear()
        if not items:
            self._set_dynamic_placeholder(sec_id, self.tr("(none)"))
            return
        for key, label, count in items:
            text = f"{label}  ({count:,})" if count is not None else label
            cb = TriStateCheckBox(text)
            cb.set_state(prev.get(key, STATE_OFF))
            cb.stateChanged.connect(self._emit)
            checks[key] = cb
            clay.addWidget(cb)

    # -- state ----------------------------------------------------------------
    def state(self) -> dict:
        st: dict = {key: cb.state() for key, cb in self._checks.items()}
        for sec_id, (_clay, checks) in self._dynamic.items():
            st[sec_id] = frozenset(
                k for k, cb in checks.items() if cb.state() == 1)
            st[sec_id + "_exclude"] = frozenset(
                k for k, cb in checks.items() if cb.state() == 2)
        return st

    def set_check(self, key: str, state: int, *, emit: bool = True) -> None:
        """Programmatically set a status checkbox's tri-state (used by the
        modlist column menu's quick-filter shortcuts). Fires `changed` by
        default so the modlist re-filters."""
        cb = self._checks.get(key)
        if cb is not None:
            cb.set_state(state, emit=emit)

    def check_state(self, key: str) -> int:
        cb = self._checks.get(key)
        return cb.state() if cb is not None else STATE_OFF

    def dynamic_state(self, sec_id: str, key: str) -> int:
        """Tri-state of one dynamic item (used by the header filter menu's
        check marks). Unknown ids/keys read as OFF."""
        entry = self._dynamic.get(sec_id)
        if entry is None:
            return STATE_OFF
        cb = entry[1].get(key)
        return cb.state() if cb is not None else STATE_OFF

    def set_dynamic_check(self, sec_id: str, key: str, state: int, *,
                          emit: bool = True) -> None:
        """Set one dynamic item's tri-state. The item must already exist (the
        host repopulates the section before driving it)."""
        entry = self._dynamic.get(sec_id)
        if entry is None:
            return
        cb = entry[1].get(key)
        if cb is not None:
            cb.set_state(state, emit=emit)

    def set_check_enabled(self, key: str, enabled: bool) -> None:
        cb = self._checks.get(key)
        if cb is not None:
            cb.setEnabled(enabled)
            if not enabled and cb.state() != STATE_OFF:
                cb.set_state(STATE_OFF, emit=False)

    def set_check_label(self, key: str, label: str) -> None:
        cb = self._checks.get(key)
        if cb is not None:
            cb.setText(label)
            cb.update()

    def clear_all(self) -> None:
        for cb in self._checks.values():
            cb.set_state(STATE_OFF, emit=False)
        for _clay, checks in self._dynamic.values():
            for cb in checks.values():
                cb.set_state(STATE_OFF, emit=False)
        self._emit()

    def any_active(self) -> bool:
        if any(cb.state() for cb in self._checks.values()):
            return True
        for _clay, checks in self._dynamic.values():
            if any(cb.state() for cb in checks.values()):
                return True
        return False

    def _emit(self) -> None:
        self.changed.emit(self.state())

    # -- styling --------------------------------------------------------------
    def _qss(self, p) -> str:
        c = lambda k: _c(p, k)
        return f"""
        #FilterPanel {{ background: {c('BG_PANEL')}; }}
        #FilterHeader {{ background: {c('BG_HEADER')}; }}
        #FilterTitle {{ font-weight: bold; font-size: 14px; color: {c('TEXT_MAIN')}; }}
        #FilterRule {{ background: {c('BORDER')}; }}
        #FilterBody {{ background: {c('BG_PANEL')}; }}
        #FilterSectionTitle {{ font-weight: bold; color: {c('TEXT_MAIN')};
                               padding-top: 4px; }}
        #FilterEmpty {{ color: {c('TEXT_DIM')}; font-style: italic; }}
        #FilterClearBtn, #FilterCloseBtn {{
            background: transparent; border: none; color: {c('TEXT_DIM')};
            padding: 2px 4px;
        }}
        #FilterClearBtn:hover, #FilterCloseBtn:hover {{ color: {c('TEXT_MAIN')}; }}
        QScrollArea {{ background: {c('BG_PANEL')}; border: none; }}
        """
