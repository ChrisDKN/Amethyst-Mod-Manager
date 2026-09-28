"""Collapsible framework-status banners shown above the plugin tabs."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QWidget, QVBoxLayout, QLabel, QPushButton, QSizePolicy

from gui_qt.icons import icon
from gui_qt.theme_qt import active_palette, bind_theme, _c
from Utils.ui.config import (
    load_framework_banners_collapsed, save_framework_banners_collapsed,
)
from Utils.games.frameworks import (
    STATE_INSTALLED, STATE_NOT_DEPLOYED, STATE_NOT_ENABLED, STATE_MISSING,
)

ROW_H = 22

# state → (bg palette key, fg palette key). Dedicated FRAMEWORK_* keys (their own
# "Framework detection" section in the theme editor); seeded from the same colours
# the shared tinted rows used, but independently editable.
_STATE_COLORS = {
    STATE_INSTALLED:    ("FRAMEWORK_INSTALLED_BG", "FRAMEWORK_INSTALLED_FG"),
    STATE_NOT_DEPLOYED: ("FRAMEWORK_STAGED_BG",    "FRAMEWORK_STAGED_FG"),
    STATE_NOT_ENABLED:  ("FRAMEWORK_DISABLED_BG",  "FRAMEWORK_DISABLED_FG"),
    STATE_MISSING:      ("FRAMEWORK_MISSING_BG",   "FRAMEWORK_MISSING_FG"),
}


class FrameworkBanner(QWidget):
    """Call `set_statuses(list[FrameworkStatus])` to (re)build the rows. Hides
    itself when the list is empty so the columns sit flush."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self._v = QVBoxLayout(self)
        self._v.setContentsMargins(0, 0, 0, 0)
        self._v.setSpacing(1)
        self._toggle = QPushButton()
        self._toggle.setCheckable(True)
        self._toggle.setChecked(not load_framework_banners_collapsed())
        self._toggle.setFixedHeight(ROW_H)
        self._toggle.setCursor(Qt.PointingHandCursor)
        p = active_palette()
        self._toggle.setStyleSheet(
            f"QPushButton {{ background:{_c(p, 'BG_HEADER')};"
            f" color:{_c(p, 'TEXT_MAIN')}; border:0; border-radius:0;"
            f" padding:0 10px; text-align:left; }}"
            f"QPushButton:hover {{ background:{_c(p, 'BG_ROW_HOVER')}; }}")
        self._v.addWidget(self._toggle)
        self._body = QWidget()
        self._rows = QVBoxLayout(self._body)
        self._rows.setContentsMargins(0, 0, 0, 0)
        self._rows.setSpacing(1)
        self._body.setVisible(self._toggle.isChecked())
        self._v.addWidget(self._body)
        self._details = ""
        self._toggle.toggled.connect(self._set_expanded)
        bind_theme(self, roles={"DROPDOWN_ARROW"})
        self._last_sig = None   # last rendered (label, state) tuple - dedup guard
        self.hide()

    def refresh_theme(self, pal: dict) -> None:
        self._arrow_color = _c(pal, "DROPDOWN_ARROW")
        self._sync_toggle()

    def _sync_toggle(self) -> None:
        expanded = self._toggle.isChecked()
        self._toggle.setIcon(icon(
            "arrow.png" if expanded else "right.png", 12,
            color=self._arrow_color))
        action = (self.tr("Collapse framework banners") if expanded
                  else self.tr("Expand framework banners"))
        self._toggle.setToolTip(action + "\n\n" + self._details)

    def _set_expanded(self, expanded: bool) -> None:
        self._body.setVisible(expanded)
        self._sync_toggle()
        save_framework_banners_collapsed(not expanded)

    def _render(self, st) -> str:
        """Translated banner text for a FrameworkStatus. The neutral detector
        builds an English `st.message`; here we re-render it from state+label so
        it's translatable (and keeps the ✔/●/✘ glyph prefix). Falls back to the
        English message for any unknown state."""
        label = st.label
        if st.state == STATE_INSTALLED:
            return self.tr("✔  {0} Installed").format(label)
        if st.state == STATE_NOT_DEPLOYED:
            return self.tr("●  {0} present in modlist but not deployed").format(label)
        if st.state == STATE_NOT_ENABLED:
            return self.tr("●  {0} present in modlist but not enabled").format(label)
        if st.state == STATE_MISSING:
            return self.tr("✘  {0} Not Present").format(label)
        return st.message

    def set_statuses(self, statuses) -> None:
        statuses = tuple(statuses or [])
        # No-op when nothing changed: deploy/restore fires ~3 banner refreshes
        # in quick succession (post-op refresh + conflict-ready + plugins-loaded)
        # and each one used to tear down the row QLabels (deleteLater) and
        # rebuild them - a visible repaint gap that read as the banner "briefly
        # disappearing". Rebuild only when the rendered rows actually differ.
        sig = tuple((s.label, s.state) for s in statuses)
        if sig == getattr(self, "_last_sig", None):
            return
        self._last_sig = sig
        # Clear existing rows.
        while self._rows.count():
            it = self._rows.takeAt(0)
            w = it.widget()
            if w is not None:
                w.hide()
                w.deleteLater()
        if not statuses:
            self.hide()
            return
        self._toggle.setVisible(len(statuses) >= 2)
        self._body.setVisible(len(statuses) == 1 or self._toggle.isChecked())
        installed = sum(st.state == STATE_INSTALLED for st in statuses)
        self._toggle.setText(self.tr("Frameworks ({0}/{1} installed)").format(
            installed, len(statuses)))
        self._details = "\n".join(self._render(st) for st in statuses)
        self._sync_toggle()
        p = active_palette()
        for st in statuses:
            bg_key, fg_key = _STATE_COLORS.get(st.state, _STATE_COLORS[STATE_MISSING])
            lbl = QLabel(self._render(st))
            lbl.setFixedHeight(ROW_H)
            lbl.setAlignment(Qt.AlignVCenter | Qt.AlignLeft)
            lbl.setStyleSheet(
                f"background:{_c(p, bg_key)}; color:{_c(p, fg_key)};"
                f" padding-left:10px;")
            self._rows.addWidget(lbl)
        self.show()
