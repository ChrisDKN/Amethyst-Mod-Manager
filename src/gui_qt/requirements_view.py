"""View Requirements panel - two side-by-side lists showing which mods the
selected mod requires ("Requires", purple) and which installed mods require it
("Required by", blue). Opens as a plugins-panel-scoped tab and follows the
modlist selection while open (the window suppresses conflict highlights and
tints the related rows with the same two colours instead).

Entirely offline: it reads the full requirements list the update checker stores
in each mod's meta.ini (`nexusRequirements`, "modId:name" pairs) - no API calls.
Requirements that aren't installed are shown dimmed with the Nexus mod name.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor, QFontMetrics
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QListWidget, QListWidgetItem,
    QFrame, QSizePolicy, QPushButton,
)

from gui_qt.theme_qt import (
    active_palette, bind_theme, contrast_text, _c, close_button,
    button_qss,
)


_DIM_ITEM_ROLE = Qt.UserRole + 37


class _ElidedLabel(QLabel):
    """A QLabel that elides with an ellipsis instead of forcing the panel wider.

    Takes a stretch factor in the header layout so it fills the space up to the
    Close button, and only elides when the text genuinely won't fit. The minimum
    width is tiny so a long name can never push the panel wider than its column
    (the ellipsis absorbs the overflow instead)."""

    def __init__(self, text=""):
        super().__init__()
        self._full = text
        self.setMinimumWidth(0)
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self._apply_elide()

    def setText(self, text):  # noqa: N802 (Qt override)
        self._full = text
        self._apply_elide()

    def _apply_elide(self):
        fm = QFontMetrics(self.font())
        super().setText(fm.elidedText(self._full, Qt.ElideRight, self.width()))

    def resizeEvent(self, event):  # noqa: N802 (Qt override)
        super().resizeEvent(event)
        self._apply_elide()


class RequirementsView(QWidget):
    """Scoped-tab body showing a mod's requirement relationships."""

    def __init__(self, staging_fn, on_close, on_data_changed=None,
                 on_focus_changed=None, on_view_missing=None):
        super().__init__()
        # staging_fn() → current staging Path (profile-switch safe) or None.
        self._staging_fn = staging_fn
        self._on_close = on_close or (lambda: None)
        # Called after every rebuild so the window can re-tint the modlist.
        self._on_data_changed = on_data_changed or (lambda: None)
        # on_focus_changed(visible: bool) - fired when this scoped tab is shown
        # or hidden (the user switches to/from another tab in the same panel).
        # The window uses it to enable requirement highlights only while the
        # tab is actually on screen, restoring conflict highlights otherwise.
        self._on_focus_changed = on_focus_changed or (lambda _v: None)
        # on_view_missing(names: list[str]) - opens the Missing Requirements
        # panel for the current selection (the window filters to mods that
        # actually have missing requirements).
        self._on_view_missing = on_view_missing or (lambda _n: None)

        # Selected mod folder names (multiple pool their requirements together).
        self.current_mods: list[str] = []
        # Installed mod FOLDER names related to the selection - the window reads
        # these to drive the purple/blue modlist highlights.
        self.installed_requires: set[str] = set()
        self.installed_required_by: set[str] = set()

        self._needs_repopulate = False
        self._repop_timer: QTimer | None = None

        self.setObjectName("RequirementsView")
        self._build()
        bind_theme(self, roles={"TEXT_DIM"})

    def refresh_theme(self, p):
        for lst in (self._requires_list, self._required_by_list):
            for i in range(lst.count()):
                item = lst.item(i)
                if item.data(_DIM_ITEM_ROLE):
                    item.setForeground(QColor(_c(p, "TEXT_DIM")))
            lst.viewport().update()

    # ---- layout -----------------------------------------------------------
    def _build(self):
        p = active_palette()
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)

        bar = QWidget(); bar.setObjectName("HeaderBar")
        hb = QHBoxLayout(bar); hb.setContentsMargins(12, 8, 8, 8); hb.setSpacing(8)
        self._title = _ElidedLabel(self.tr("Requirements"))
        self._title.setStyleSheet(f"color:{_c(p,'TEXT_MAIN')}; font-weight:600;")
        # The title takes the stretch so it fills the space up to the Close
        # button and only elides when the name truly won't fit.
        hb.addWidget(self._title, 1)
        close = close_button(pal=p)
        close.clicked.connect(lambda: self._on_close())
        hb.addWidget(close, 0)
        v.addWidget(bar)

        # Hint label for the no-data / no-selection states.
        self._status = QLabel("")
        self._status.setStyleSheet(f"color:{_c(p,'TEXT_DIM')}; padding:10px 12px;")
        self._status.setWordWrap(True)
        v.addWidget(self._status)

        # Two columns, each a coloured header over a read-only list.
        body = QWidget()
        cols = QHBoxLayout(body)
        cols.setContentsMargins(8, 8, 8, 8)
        cols.setSpacing(8)
        self._requires_list = self._add_column(
            cols, p, self.tr("Requires"), _c(p, "REQ_HL_REQUIRES"))
        self._required_by_list = self._add_column(
            cols, p, self.tr("Required by"), _c(p, "REQ_HL_REQUIRED_BY"))
        self._body = body
        v.addWidget(body, 1)

        # Bottom bar: open the Missing Requirements panel for the selection.
        foot = QWidget(); foot.setObjectName("FooterBar")
        fb = QHBoxLayout(foot); fb.setContentsMargins(8, 6, 8, 8); fb.setSpacing(8)
        fb.addStretch(1)
        self._missing_btn = QPushButton(self.tr("View Missing Requirements"))
        self._missing_btn.setCursor(Qt.PointingHandCursor)
        self._missing_btn.setStyleSheet(button_qss("BTN_WARN", pal=p))
        self._missing_btn.clicked.connect(
            lambda: self._on_view_missing(list(self.current_mods)))
        fb.addWidget(self._missing_btn, 0)
        self._foot = foot
        v.addWidget(foot)

    def _add_column(self, cols, p, label, header_bg) -> QListWidget:
        col = QVBoxLayout(); col.setContentsMargins(0, 0, 0, 0); col.setSpacing(0)
        head = QLabel(label)
        head.setAlignment(Qt.AlignCenter)
        head.setStyleSheet(
            f"background:{header_bg};"
            f" color:{contrast_text(header_bg)};"
            f" font-weight:600; padding:6px;"
            f" border:1px solid {_c(p,'BORDER')}; border-bottom:none;")
        col.addWidget(head)
        lst = QListWidget()
        lst.setFrameShape(QFrame.NoFrame)
        lst.setSelectionMode(QListWidget.NoSelection)
        lst.setFocusPolicy(Qt.NoFocus)
        lst.setStyleSheet(
            f"QListWidget{{background:{_c(p,'BG_PANEL')};"
            f" color:{_c(p,'TEXT_MAIN')};"
            f" border:1px solid {_c(p,'BORDER')};}}")
        col.addWidget(lst, 1)
        cols.addLayout(col, 1)
        return lst

    # ---- selection tracking -------------------------------------------------
    def show_mods(self, mod_names):
        """Retarget the panel to the selected mods (empty = nothing selected).
        Several mods pool their requirements together. The rebuild is deferred
        off the selection handler's stack - same pattern as the Mod Files tab."""
        self.current_mods = list(mod_names or ())
        self._request_repopulate()

    def show_mod(self, mod_name):
        """Back-compat single-mod entry point (delegates to show_mods)."""
        self.show_mods([mod_name] if mod_name else [])

    def _request_repopulate(self):
        if not self.isVisible():
            self._needs_repopulate = True
            return
        t = self._repop_timer
        if t is None:
            t = QTimer(self)
            t.setSingleShot(True)
            t.setInterval(0)
            t.timeout.connect(self._repopulate)
            self._repop_timer = t
        t.start()

    def showEvent(self, event):  # noqa: N802 (Qt override)
        super().showEvent(event)
        if self._needs_repopulate:
            self._needs_repopulate = False
            self._repopulate()
        # Tab came to the front → the window turns requirement highlights on.
        self._on_focus_changed(True)

    def hideEvent(self, event):  # noqa: N802 (Qt override)
        super().hideEvent(event)
        # Tab was backgrounded (user switched to another tab in this panel) →
        # the window restores the normal conflict highlights.
        self._on_focus_changed(False)

    # ---- rebuild ------------------------------------------------------------
    def _repopulate(self):
        self._requires_list.clear()
        self._required_by_list.clear()
        self.installed_requires = set()
        self.installed_required_by = set()

        names = list(self.current_mods)
        if len(names) == 1:
            title = self.tr("Requirements - {0}").format(names[0])
        elif names:
            title = self.tr("Requirements - {0} mods").format(len(names))
        else:
            title = self.tr("Requirements")
        self._title.setText(title)
        self._title.setToolTip(title)

        staging = self._staging_fn() if self._staging_fn else None
        if not names or staging is None:
            self._set_hint(self.tr("Select one or more mods."))
            return

        from Nexus.nexus_meta import read_meta, scan_installed_mods, parse_req_pairs
        selected_set = set(names)
        metas = [read_meta(staging / n / "meta.ini") for n in names]
        # Only Nexus mods carry requirement data - filter out the rest but keep
        # going as long as at least one selected mod is Nexus-backed.
        nexus_metas = [m for m in metas if m.mod_id > 0]
        if not nexus_metas:
            self._set_hint(self.tr("No Nexus data for the selected mod(s)."))
            return
        self._set_hint(None)

        # The Missing Requirements button is only useful when the selection
        # actually has stored missing requirements - enable it accordingly.
        has_missing = any(m.missing_requirements for m in nexus_metas)
        self._missing_btn.setEnabled(has_missing)
        self._missing_btn.setToolTip(
            "" if has_missing
            else self.tr("No missing requirements for the selected mod(s)."))

        selected_ids = {m.mod_id for m in nexus_metas}

        all_metas = scan_installed_mods(staging)
        by_id: dict[int, list[str]] = {}
        for m in all_metas:
            by_id.setdefault(m.mod_id, []).append(m.mod_name)

        # ---- Requires: union of every selected mod's stored requirements,
        # deduped by requirement id. "Requires this data" only when NONE of the
        # selected mods have been update-checked (a checked mod with genuinely
        # no requirements just contributes nothing to the pool).
        any_req_data = any(m.nexus_requirements for m in nexus_metas)
        seen_req: set[int] = set()
        for m in nexus_metas:
            for rid, rname in parse_req_pairs(m.nexus_requirements):
                # External requirements (id 0 - off-Nexus tools like SKSE/ENB/
                # Nemesis) aren't actionable here (no folder to map, nothing to
                # install), so they're left out of the list entirely.
                if rid <= 0:
                    continue
                if rid in seen_req:  # dedup across the pooled selection
                    continue
                seen_req.add(rid)
                # A selected mod that another selected mod requires isn't an
                # external dependency to show - it's already in the anchor set.
                if rid in selected_ids:
                    continue
                if rid in by_id:
                    for folder in by_id[rid]:
                        if folder in selected_set:
                            continue
                        self._add_row(self._requires_list, folder, dim=False)
                        self.installed_requires.add(folder)
                else:
                    self._add_row(self._requires_list,
                                  self.tr("{0}  (not installed)").format(rname),
                                  dim=True)
        if self._requires_list.count() == 0:
            self._add_row(
                self._requires_list,
                self.tr("(none)") if any_req_data
                else self.tr("Run Check Updates for this data."), dim=True)

        # ---- Required by: installed mods whose stored list names ANY of the
        # selected mods' ids (union). Derived from the OTHER mods' data, so it
        # works even when the selection has no requirements of its own.
        have_data = False
        for m in all_metas:
            if m.mod_name in selected_set:
                continue
            if not m.nexus_requirements:
                continue
            have_data = True
            req_ids = {rid for rid, _ in parse_req_pairs(m.nexus_requirements)}
            if req_ids & selected_ids:
                self._add_row(self._required_by_list, m.mod_name, dim=False)
                self.installed_required_by.add(m.mod_name)
        if self._required_by_list.count() == 0:
            # Distinguish "no dependents" from "no other mod has been checked
            # yet" - otherwise "(none)" would be misleading before any check.
            self._add_row(
                self._required_by_list,
                self.tr("(none)") if have_data
                else self.tr("Run Check Updates for this data."),
                dim=True)

        self._on_data_changed()

    def _add_row(self, lst: QListWidget, text: str, *, dim: bool):
        it = QListWidgetItem(text)
        it.setData(_DIM_ITEM_ROLE, dim)
        if dim:
            it.setForeground(QColor(_c(active_palette(), "TEXT_DIM")))
        it.setToolTip(text)
        lst.addItem(it)

    def _set_hint(self, text: str | None):
        """Show a hint instead of the lists (or the lists when *text* is None).
        Highlights are already cleared by the reset at the top of _repopulate -
        the on_data_changed callback pushes the (empty) sets to the window."""
        if text is None:
            self._status.setVisible(False)
            self._body.setVisible(True)
            self._foot.setVisible(True)
            return
        self._status.setText(text)
        self._status.setVisible(True)
        self._body.setVisible(False)
        # Nothing selectable/actionable → hide the footer button too.
        self._foot.setVisible(False)
        self._on_data_changed()
