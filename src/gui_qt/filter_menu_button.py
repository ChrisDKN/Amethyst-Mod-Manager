"""Eye button pinned to the left of a tree's column header that pops the tab's
filters as a menu - one submenu per filter category.

The modlist and plugins views grow their own button (theirs also carries column
show/hide); the simpler tabs - Mod Files, Text Files, Saves, Data, Downloads -
share this one. It is purely a shortcut onto the tab's FilterSidePanel: the host
supplies

    sections_fn() -> [{"title": str, "entries": [(key, label, tri_state)]}]
    on_toggle(key, on: bool)
    on_clear()            (optional)
    any_active() -> bool  (optional; greys out "Clear all filters")

with `key` opaque to this widget, so panel and menu always show the same state.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, QSize, QEvent
from PySide6.QtGui import QAction
from PySide6.QtWidgets import QToolButton, QListWidget, QWidgetAction

from gui_qt.theme_qt import bind_theme, _c

BTN_W = 26

# Past this many entries a category becomes one scrollable checklist instead of
# a wall of actions (same treatment as the modlist's Move-to-separator submenu:
# QMenu's own scroller only engages past the SCREEN height, and a max-height
# QMenu just clips and mis-positions).
SCROLL_CAP = 15


class FilterMenuButton(QToolButton):
    """Header-left eye button opening the tab's filters as a nested menu."""

    def __init__(self, tree):
        header = tree.header()
        super().__init__(header)
        from gui_qt.icons import icon
        from gui_qt.theme_qt import active_palette, _c
        self._header = header
        self.sections_fn = None
        self.on_toggle = None
        self.on_clear = None
        self.any_active = None
        p = active_palette()
        # Tint the eye glyph to the theme foreground so it reads in both light
        # and dark modes (the white PNG is invisible on the light header).
        self.setIcon(icon("eye1_white.png", 16, color=_c(p, "TEXT_MAIN")))
        self.setIconSize(QSize(16, 16))
        self.setCursor(Qt.ArrowCursor)
        self.setFocusPolicy(Qt.NoFocus)
        self.setAutoRaise(True)
        self.setToolTip(self.tr("Filters"))
        # Opaque header-coloured background: the header paints its first section
        # shifted right by BTN_W (see TkStyleHeader.header_left_pad), so this
        # button covers the strip that shift leaves bare.
        self.setStyleSheet(
            f"QToolButton {{ background: {_c(p, 'BG_HEADER')};"
            " border: none; padding: 0px; }")
        self.clicked.connect(self._show_menu)
        header.installEventFilter(self)
        self.reposition()
        self.show()
        bind_theme(self, roles={"TEXT_MAIN"})

    def refresh_theme(self, p: dict) -> None:
        from gui_qt.icons import icon
        self.setIcon(icon("eye1_white.png", 16, color=_c(p, "TEXT_MAIN")))

    # -- geometry -----------------------------------------------------------
    def eventFilter(self, obj, event):
        if obj is self._header and event.type() in (QEvent.Resize, QEvent.Show):
            self.reposition()
        return super().eventFilter(obj, event)

    def reposition(self):
        try:
            height = self._header.height()
        except RuntimeError:
            return          # header torn down (tab closed / app shutdown)
        self.setGeometry(0, 0, BTN_W, height)
        self.raise_()

    # -- menu ---------------------------------------------------------------
    def _show_menu(self):
        from gui_qt.modlist_view import _StayOpenMenu
        menu = _StayOpenMenu(self)
        sections = self.sections_fn() if callable(self.sections_fn) else []
        for sec in sections:
            sub = _StayOpenMenu(sec.get("title", ""), menu)
            entries = sec.get("entries") or []
            if not entries:
                empty = sub.addAction(self.tr("(none)"))
                empty.setEnabled(False)
            elif len(entries) > SCROLL_CAP:
                self._fill_scroll_section(sub, entries)
            else:
                for key, label, state in entries:
                    a = QAction(label, sub)
                    a.setCheckable(True)
                    # Tri-state 1 = include; 2 (exclude) is panel-only, so it
                    # reads here as unchecked rather than silently flipping to
                    # include.
                    a.setChecked(state == 1)
                    a.toggled.connect(
                        lambda checked, k=key: self._toggle(k, checked))
                    sub.addAction(a)
            menu.addMenu(sub)
        menu.addSeparator()
        clear = QAction(self.tr("Clear all filters"), menu)
        clear.setEnabled(not callable(self.any_active) or self.any_active())
        clear.triggered.connect(self._clear)
        menu.addAction(clear)
        menu.exec(self.mapToGlobal(self.rect().bottomLeft()))

    def _fill_scroll_section(self, sub, entries):
        """Hold *entries* in one scrollable checklist capped at SCROLL_CAP rows.
        Toggling a row leaves the menu open, exactly like the plain actions."""
        lst = _CheckList(sub, self._toggle)
        lst.setStyleSheet(_checklist_qss())
        for key, label, state in entries:
            lst.add_entry(key, label, state == 1)
        row_h = lst.sizeHintForRow(0) or 22
        lst.setFixedHeight(row_h * SCROLL_CAP + 4)
        sbar_w = lst.verticalScrollBar().sizeHint().width()
        lst.setMinimumWidth(lst.sizeHintForColumn(0) + sbar_w + 40)
        wa = QWidgetAction(sub)
        wa.setDefaultWidget(lst)
        sub.addAction(wa)

    def _toggle(self, key, on: bool):
        if callable(self.on_toggle):
            self.on_toggle(key, on)

    def _clear(self):
        if callable(self.on_clear):
            self.on_clear()


class _CheckList(QListWidget):
    """Checkable list standing in for a long submenu's actions. Clicking
    ANYWHERE on a row flips it (Qt would only react on the indicator), so we own
    the release and never chain to the base class's own toggle."""

    def __init__(self, parent, on_toggle):
        super().__init__(parent)
        self._on_toggle = on_toggle
        self._keys: list = []
        self.setFrameShape(QListWidget.Shape.NoFrame)
        self.setSelectionMode(QListWidget.SelectionMode.NoSelection)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.setMouseTracking(True)

    def add_entry(self, key, label: str, checked: bool) -> None:
        from PySide6.QtWidgets import QListWidgetItem
        item = QListWidgetItem(label, self)
        item.setCheckState(Qt.Checked if checked else Qt.Unchecked)
        self._keys.append(key)

    def mouseReleaseEvent(self, event):
        item = self.itemAt(event.position().toPoint())
        if item is not None:
            on = item.checkState() != Qt.Checked
            item.setCheckState(Qt.Checked if on else Qt.Unchecked)
            row = self.row(item)
            if 0 <= row < len(self._keys):
                self._on_toggle(self._keys[row], on)
            return
        super().mouseReleaseEvent(event)


def _checklist_qss() -> str:
    """Menu-like look for the scrollable checklist: transparent rows with a
    hover highlight and the same blue check box QMenu's checkable items use."""
    from gui_qt.theme_qt import (active_palette, _c, contrast_text,
                                 _tinted_icon_url)
    p = active_palette()
    fill = _c(p, "CHECK_FILL")
    tick = _tinted_icon_url("check_white.png", contrast_text(fill))
    return f"""
    QListWidget {{ background: transparent; outline: none; border: none; }}
    QListWidget::item {{ padding: 5px 12px 5px 2px; border-radius: 4px; }}
    QListWidget::item:hover {{
        background: {_c(p, 'BG_SELECT')}; color: {_c(p, 'TEXT_ON_ACCENT')}; }}
    QListWidget::indicator {{ width: 16px; height: 16px; margin-left: 6px; }}
    QListWidget::indicator:unchecked {{
        border: 1px solid {_c(p, 'BORDER_FAINT')}; border-radius: 3px;
        background: {_c(p, 'BG_DEEP')}; }}
    QListWidget::indicator:checked {{
        border: 1px solid {fill}; border-radius: 3px;
        background: {fill}; image: url({tick}); }}
    """
