"""SelectorButton - a dropdown that shows the current selection and exposes a
list of choices plus pinned action items at the bottom of the menu.

Used to consolidate the top bar's game and profile controls: one button each,
instead of a +/⚙/combo cluster. Selecting a list item changes the current
choice (fires on_select); the bottom action items fire their own callbacks and
never become the selection.
"""

from __future__ import annotations

from typing import Callable
from PySide6.QtWidgets import QToolButton, QMenu, QListWidget
from PySide6.QtGui import QActionGroup
from PySide6.QtCore import Qt, QSize, QEvent, QObject, QTimer, Signal


class SplitPressHighlighter(QObject):
    """Event filter for split (MenuButtonPopup) buttons whose QSS lights the
    arrow section via the `menuOpen` dynamic property.

    Qt repaints the pressed (sunken) body SYNCHRONOUSLY inside
    mousePressEvent, before any pressed-signal handler runs - so when menuOpen
    is only set in the menu's aboutToShow, the body turns blue a whole
    menu-build ahead of the arrow (very visible on big dynamic menus like
    Wizard, whose aboutToShow rebuild probes the filesystem). QSS can't close
    the gap either: `:pressed::menu-button` misparses and leaks onto every
    state. Setting the property here, before the widget sees the press, folds
    both halves into that one synchronous repaint. aboutToHide still clears
    it; the release guard covers a menu that never actually showed."""

    def eventFilter(self, btn, ev):
        t = ev.type()
        if t == QEvent.MouseButtonPress and ev.button() == Qt.LeftButton:
            self._set(btn, True)
        elif t == QEvent.MouseButtonRelease:
            menu = btn.menu()
            if menu is None or not menu.isVisible():
                self._set(btn, False)
        return False

    @staticmethod
    def _set(btn, on: bool):
        if btn.property("menuOpen") == on:
            return
        btn.setProperty("menuOpen", on)
        btn.style().unpolish(btn)
        btn.style().polish(btn)


class _StayOpenMenu(QMenu):
    """A QMenu that does NOT close when a checkable/enabled action is clicked -
    the action is triggered (toggling it) but the menu stays open so several
    options can be flipped in one visit. Non-checkable actions (e.g. a nested
    submenu title or a normal command) close it as usual."""

    def mouseReleaseEvent(self, event):
        act = self.activeAction()
        if (act is not None and act.isEnabled() and act.isCheckable()
                and act.menu() is None):
            # Toggle + fire the action ourselves, then swallow the event so the
            # base class never runs its close-the-menu path.
            act.trigger()
            return
        super().mouseReleaseEvent(event)


def _item_list_qss() -> str:
    """Menu-like look for the scrollable item list: transparent rows with the
    QMenu hover highlight, and a faint tint on the current selection."""
    from gui_qt.theme_qt import active_palette, _c
    p = active_palette()
    return f"""
    QListWidget {{ background: transparent; outline: none; border: none; }}
    QListWidget::item {{ padding: 7px 12px 7px 6px; border-radius: 4px;
                         margin: 1px 2px; color: {_c(p, 'TEXT_MAIN')}; }}
    QListWidget::item:selected {{
        background: {_c(p, 'BG_ROW_ALT')}; color: {_c(p, 'TEXT_MAIN')}; }}
    QListWidget::item:hover {{
        background: {_c(p, 'BG_SELECT')}; color: {_c(p, 'TEXT_ON_ACCENT')}; }}
    """


class _ItemList(QListWidget):
    """The selector's choices as one scrollable list, standing in for a run of
    plain menu actions once they outgrow the button's scroll cap - the pinned
    actions below it then stay on screen no matter how many items there are.

    Rows read like menu items (icon, hover highlight, click picks and closes);
    the current selection is the one selected row. Picking is deferred to the
    event loop because the callback rebuilds the menu, which deletes this
    widget - it must not happen inside its own event handler."""

    def __init__(self, menu, on_pick):
        super().__init__(menu)
        self._menu = menu
        self._on_pick = on_pick
        self._labels: list[str | None] = []      # None = separator row
        self.current_row = -1
        self.setFrameShape(QListWidget.Shape.NoFrame)
        self.setSelectionMode(QListWidget.SelectionMode.SingleSelection)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.setMouseTracking(True)
        # Focus stays with the menu (its keyboard navigation still reaches the
        # pinned actions); the list is mouse/wheel driven.
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)

    def add_row(self, label: str, icon, text: str, bold: bool, current: bool):
        from PySide6.QtWidgets import QListWidgetItem
        item = QListWidgetItem(text, self)
        if icon is not None:
            item.setIcon(icon)
        if bold:
            f = item.font(); f.setBold(True); item.setFont(f)
        self._labels.append(label)
        if current:
            self.current_row = self.count() - 1
            item.setSelected(True)

    def add_separator(self):
        from PySide6.QtWidgets import QFrame, QListWidgetItem
        from gui_qt.theme_qt import active_palette, _c
        item = QListWidgetItem(self)
        item.setFlags(Qt.NoItemFlags)
        item.setSizeHint(QSize(0, 9))
        # A hairline row widget - QMenu::separator can't reach inside the list.
        line = QFrame(self)
        line.setFixedHeight(1)
        line.setStyleSheet(f"background: {_c(active_palette(), 'BORDER')};")
        self.setItemWidget(item, line)
        self._labels.append(None)

    def row_height(self) -> int:
        """Tallest ordinary row - separators are shorter, so a plain max over
        the first few rows can't be fooled by one landing at index 0."""
        hints = [self.sizeHintForRow(i) for i in range(min(self.count(), 8))]
        return max([h for h in hints if h > 0] or [24])

    def mouseReleaseEvent(self, event):
        item = self.itemAt(event.position().toPoint())
        row = self.row(item) if item is not None else -1
        label = self._labels[row] if 0 <= row < len(self._labels) else None
        if label is None:
            super().mouseReleaseEvent(event)
            return
        self._menu.close()
        QTimer.singleShot(0, lambda l=label: self._on_pick(l))


class SelectorButton(QToolButton):
    # Fired whenever the face changes (selection, item icons, item list). A
    # responsive top bar re-runs its width budget on it - a longer game or
    # profile name may not fit where the previous one did.
    face_changed = Signal()

    def __init__(self, *, items=None, current=None, actions=None,
                 on_select: "Callable[[str], None] | None" = None,
                 prefix="", min_width=170, icon=None, icon_px=18,
                 item_icons=None, icon_provider=None, scroll_after=None,
                 parent=None):
        """*items*   - list of selectable labels.
        *current*   - initially selected label (defaults to items[0]).
        *actions*   - list of (label, callback) pinned below a separator.
        *on_select* - called with the chosen label when a list item is picked.
        *prefix*    - text shown before the current label on the button itself
                      (e.g. "Profile: "); not part of the selectable values.
        *icon*      - a QIcon to show INSTEAD of the current-label text (the
                      button becomes an icon button; the menu is unchanged).
        *item_icons* - {label: QIcon} shown next to each menu entry, and on the
                      button face beside the current label (text stays). Only
                      used in text mode (ignored when *icon* replaces the text).
        *icon_provider* - called with a label to supply its item icon on demand
                      (return None for "no icon"). Fills the gaps in
                      *item_icons* at rebuild, so callers that only ever pass an
                      item LIST still get icons.
        *scroll_after* - past this many items the list becomes one scrollable
                      block that tall, keeping the pinned actions in view
                      instead of pushing them down the screen. None = never.
        """
        super().__init__(parent)
        self._items: list[str] = list(items or [])
        self._actions = list(actions or [])
        self._on_select = on_select
        self._prefix = prefix
        self._icon = icon
        self._item_icons: dict = dict(item_icons or {})
        self._icon_provider = icon_provider
        self._item_icon_px = icon_px
        self._scroll_after = scroll_after
        self._item_list: _ItemList | None = None
        # Narrow-bar compaction (see set_label_width / set_icon_only) and the
        # width measurements it runs on.
        self._min_width = min_width
        self._label_cap: int | None = None
        self._icon_only = False
        self._chrome_px: int | None = None
        self._chrome_dirty = False
        self._icon_w: int | None = None
        self._measuring = False
        self._current = current or (self._items[0] if self._items else "")
        self._highlighted: str | None = None   # green "active/deployed" item
        self._item_separators: set = set()     # labels with a separator before
        self.setObjectName("ActionButton")   # share the flat toolbar styling
        self.setProperty("split", True)       # gets the arrow-room padding
        # Split button: a text section + a separate arrow section on the right
        # (the whole thing opens the menu, like the mockup's Proton dropdown).
        self.setPopupMode(QToolButton.MenuButtonPopup)
        self.setCursor(Qt.PointingHandCursor)
        if icon is not None:
            self.setIcon(icon)
            self.setIconSize(QSize(icon_px, icon_px))
            self.setToolButtonStyle(Qt.ToolButtonIconOnly)
            # Icon-only selector (e.g. the play-bar gear): no split arrow
            # section - the split QSS padding (28px arrow room) would leave no
            # space for the glyph in a square button. InstantPopup keeps the
            # whole face clickable; hide the default menu-indicator overlay.
            self.setProperty("split", False)
            self.setPopupMode(QToolButton.InstantPopup)
            self.setStyleSheet(
                "QToolButton { padding: 0; }"
                "QToolButton::menu-indicator { image: none; }")
        else:
            self.setToolButtonStyle(Qt.ToolButtonTextOnly)
            self.setMinimumWidth(min_width)
        self._menu = QMenu(self)
        self.setMenu(self._menu)
        # The text section (left of the split) also opens the menu - a selector
        # has no separate primary action. Open on *press* (like the arrow
        # section does natively). The SplitPressHighlighter sets menuOpen
        # before Qt's synchronous sunken repaint, so both halves light up in
        # the SAME frame instead of the arrow lagging one menu-build behind
        # the body. (InstantPopup - the icon-only mode - already opens on
        # press natively and has no separate arrow section.)
        if icon is None:
            self.pressed.connect(self.showMenu)
            self.installEventFilter(SplitPressHighlighter(self))
        # A dynamic `menuOpen` property (toggled while the menu is shown) drives
        # the open-state highlight in QSS - reliable across QStyles, unlike the
        # :pressed/:on pseudo-states for a MenuButtonPopup tool button.
        self.setProperty("menuOpen", False)
        self._menu.aboutToShow.connect(lambda: self._set_menu_open(True))
        self._menu.aboutToHide.connect(lambda: self._set_menu_open(False))
        # Connected once (the menu outlives every rebuild): re-reads the theme
        # colours and parks the scrollable list on the current item.
        self._menu.aboutToShow.connect(self._prep_item_list)
        self._group: QActionGroup | None = None
        self._rebuild()

    def _set_menu_open(self, on: bool):
        self.setProperty("menuOpen", on)
        # Re-evaluate the stylesheet against the new property value.
        self.style().unpolish(self)
        self.style().polish(self)

    # -- public API ---------------------------------------------------------
    def set_items(self, items, current=None, item_icons=None,
                  separator_before=None):
        """*separator_before* - labels that get a menu separator inserted
        BEFORE them (e.g. the first Profile Group, splitting groups from
        plain profiles). None keeps the previous set."""
        self._items = list(items)
        if item_icons is not None:
            self._item_icons = dict(item_icons)
        if separator_before is not None:
            self._item_separators = set(separator_before)
        if current is not None:
            self._current = current
        elif self._current not in self._items and self._items:
            self._current = self._items[0]
        self._rebuild()

    def set_item_icons(self, item_icons: dict):
        """Replace the {label: QIcon} map without touching the item list."""
        self._item_icons = dict(item_icons or {})
        self._rebuild()

    def set_actions(self, actions):
        """Replace the pinned action entries (below the item separator) and
        rebuild the menu. Each entry is (label, cb) or (label, cb, opts) where
        cb is a callable, a list of nested entries (→ a submenu), or None (a
        disabled/header row). *opts* is an optional dict with any of:
          checkable - draw a check indicator; checked reflects `checked`
          checked   - initial checked state
          group     - a hashable id; entries sharing it become mutually
                      exclusive (radio) within their menu
          separator_after - add a separator after this entry."""
        self._actions = list(actions or [])
        self._rebuild()

    def current(self) -> str:
        return self._current

    def set_current(self, label: str):
        if label in self._items:
            self._current = label
            self._rebuild()

    def set_highlighted_item(self, label: str | None):
        """Mark one item as 'active' - its menu entry is coloured green and, when
        it's also the current selection, the button itself goes green. Used to
        show the deployed profile (or active game). None clears it."""
        if getattr(self, "_highlighted", None) != label:
            self._highlighted = label
            self._rebuild()

    # -- responsive width (narrow top bar) ----------------------------------
    # The top bar hands out its width in stages: a selector's label is elided
    # (set_label_width) before the action buttons lose their own labels, and the
    # game selector falls back to its logo (set_icon_only) before that. See
    # MainWindow._sync_header_compact.

    def full_text(self) -> str:
        """The unelided button label (prefix + current item)."""
        return self.tr("{0}{1}").format(self._prefix, self._current or "-")

    def natural_width(self) -> int:
        """Layout width with the full label - its minimum width floors it."""
        return max(self._min_width,
                   self._text_chrome()
                   + self.fontMetrics().horizontalAdvance(self.full_text()))

    def current_width(self) -> int:
        """Layout width in the present state (full / elided / icon-only)."""
        return max(self.minimumWidth(), self.sizeHint().width())

    def icon_width(self) -> int:
        """Layout width in icon-only mode; 0 when the current item has no icon
        to fall back on (collapsing it would leave a blank button)."""
        if self._icon is not None or self._item_icons.get(self._current) is None:
            return 0
        if self._icon_w is None:
            self._text_chrome()     # cache the text metrics while we still can
            # Only exact with the icon-only QSS applied (different paddings), so
            # measure through a real flip instead of adding the theme up by hand.
            self._measuring = True
            prev, self._icon_only = self._icon_only, True
            self._apply_face()
            self._icon_w = self.sizeHint().width()
            self._icon_only = prev
            self._apply_face()
            self._measuring = False
        return self._icon_w

    def is_icon_only(self) -> bool:
        return self._icon_only

    def set_label_width(self, px: int | None) -> None:
        """Cap the button at *px*, eliding the label to fit; None restores it."""
        if px is not None:
            px = max(px, 48)
        if px == self._label_cap:
            return
        self._label_cap = px
        self._apply_face()

    def set_icon_only(self, on: bool) -> None:
        """Show the current item's icon INSTEAD of its label. Ignored when that
        item has no icon."""
        on = bool(on) and self._item_icons.get(self._current) is not None
        if on == self._icon_only:
            return
        if on:
            self._text_chrome()     # measurable in text mode only
        self._icon_only = on
        self._apply_face()

    def _text_chrome(self) -> int:
        """Width the button needs BEYOND its label: paddings, border and the
        split arrow section. Read back off a live sizeHint (the numbers live in
        the theme's QSS) and cached until the font or style changes."""
        if self._icon_only:
            # Not measurable in icon-only mode (different paddings, no label) -
            # the cached value from text mode is the best we have.
            return self._chrome_px or 0
        if self._chrome_px is None or self._chrome_dirty:
            self.ensurePolished()
            self._chrome_px = max(
                0, self.sizeHint().width()
                - self.fontMetrics().horizontalAdvance(self.text()))
            self._chrome_dirty = False
        return self._chrome_px

    def _apply_face(self) -> None:
        """Put the current compaction state on the button face."""
        if self._icon is not None:
            return              # fixed-icon selector - its face never changes
        full = self.full_text()
        face = self._item_icons.get(self._current)
        if self._icon_only and face is not None:
            self.setIcon(face)
            self.setIconSize(QSize(self._item_icon_px, self._item_icon_px))
            self.setToolButtonStyle(Qt.ToolButtonIconOnly)
            # Same QSS hook the action buttons use for their icon-only mode:
            # drops the label padding, keeps the arrow section.
            self.setProperty("compact", True)
            self.setToolTip(full)
            self._repolish()
            self._pin_minimum()
            return
        # The current item's icon is drawn ourselves in paintEvent (to the left
        # of the still-centred text). Keep the QToolButton in text-only mode so
        # Qt centres the label; its built-in icon slot would left-align the
        # icon+text group instead.
        self.setToolButtonStyle(Qt.ToolButtonTextOnly)
        self.setProperty("compact", False)
        cap = self._label_cap
        if cap is None:
            # No trailing glyph - the split-button's arrow section shows it now.
            self.setText(full)
            self.setMinimumWidth(self._min_width)
            self.setToolTip("")
            self._repolish()
            return
        # Drop the prefix before eliding the name itself: "Gate_To_Sovn…" says
        # more in the same pixels than "Profile: Gate_To…".
        room = max(0, cap - self._text_chrome())
        fm = self.fontMetrics()
        if fm.horizontalAdvance(full) <= room:
            text = full
        else:
            name = self._current or "-"
            text = (name if fm.horizontalAdvance(name) <= room
                    else fm.elidedText(name, Qt.ElideRight, room))
        self.setText(text)
        self.setToolTip("" if text == full else full)
        self._repolish()
        self._pin_minimum()

    def _pin_minimum(self) -> None:
        """Floor the compacted button at exactly what it now needs. Left at the
        full min_width the layout couldn't shrink it at all; left at 0 it
        becomes the one elastic item in the bar and gets squeezed to nothing
        once everything else is collapsed too."""
        self.setMinimumWidth(0)         # drop the old floor before re-reading
        self.setMinimumWidth(self.sizeHint().width())

    def _repolish(self) -> None:
        self.style().unpolish(self); self.style().polish(self)

    def changeEvent(self, e):               # noqa: N802
        # Font or theme change → the cached widths no longer describe the
        # button. (Skipped mid-measure: _apply_face repolishes as it flips.)
        # getattr: Qt can deliver this before __init__ has set the fields.
        if (e.type() in (QEvent.FontChange, QEvent.StyleChange,
                         QEvent.ApplicationFontChange)
                and not getattr(self, "_measuring", True)):
            self._chrome_dirty = True
            self._icon_w = None
        super().changeEvent(e)

    # -- internals ----------------------------------------------------------
    def _rebuild(self):
        if self._icon_provider is not None:
            for label in self._items:
                if label not in self._item_icons:
                    ic = self._icon_provider(label)
                    if ic is not None:
                        self._item_icons[label] = ic
        # A new current item can be wider/narrower, or lose its icon entirely.
        self._icon_w = None
        self._apply_face()
        # Tint the button green (via the `deployed` property) when the current
        # selection IS the highlighted/active item; QSS reads the property.
        self.setProperty("deployed", self._highlighted is not None
                         and self._current == self._highlighted)
        self.style().unpolish(self); self.style().polish(self)
        # Item icons replace the radio indicator in their rows (Qt draws the
        # action icon in the check column). icon_px=16 matches the styled
        # indicator geometry (theme_qt QMenu::indicator) so icon rows align
        # with radio rows - the profile selector uses that; the play bar
        # deliberately uses larger icons.
        self._menu.setStyleSheet(
            f"QMenu {{ icon-size: {self._item_icon_px}px; }}"
            if self._item_icons else "")
        self._menu.clear()
        self._item_list = None      # cleared with the menu
        # Exclusive action group → the selectable items render as radio buttons.
        self._group = QActionGroup(self._menu)
        self._group.setExclusive(True)
        if (self._scroll_after is not None
                and len(self._items) > self._scroll_after):
            self._add_item_list()
        else:
            for label in self._items:
                if label in self._item_separators:
                    self._menu.addSeparator()
                a = self._menu.addAction(label)
                a.setCheckable(True)
                a.setChecked(label == self._current)
                item_icon = self._item_icons.get(label)
                if item_icon is not None:
                    a.setIcon(item_icon)
                if self._highlighted is not None and label == self._highlighted:
                    # QAction can't set foreground colour; mark the deployed item
                    # with a green check + bold so it reads as active in the list.
                    a.setText(self._item_text(label))
                    f = a.font(); f.setBold(True); a.setFont(f)
                self._group.addAction(a)
                a.triggered.connect(lambda _=False, l=label: self._choose(l))
        if self._items and self._actions:
            self._menu.addSeparator()
        self._add_actions(self._menu, self._actions)
        self.face_changed.emit()

    def _item_text(self, label: str) -> str:
        """Row text for *label* - the highlighted (deployed) item says so."""
        if self._highlighted is not None and label == self._highlighted:
            return self.tr("{0}   ✓ deployed").format(label)
        return label

    def _add_item_list(self):
        """Add the selectable items as one scrollable list capped at
        _scroll_after rows, so the pinned actions below stay in view."""
        from PySide6.QtWidgets import QWidgetAction
        lst = _ItemList(self._menu, self._choose)
        lst.setIconSize(QSize(self._item_icon_px, self._item_icon_px))
        lst.setStyleSheet(_item_list_qss())
        for label in self._items:
            if label in self._item_separators:
                lst.add_separator()
            lst.add_row(label, self._item_icons.get(label),
                        self._item_text(label),
                        bold=label == self._highlighted,
                        current=label == self._current)
        lst.setFixedHeight(lst.row_height() * self._scroll_after + 4)
        # Room for the text, the scrollbar the cap guarantees, and the row
        # padding/margins the QSS above adds around both.
        lst.setMinimumWidth(lst.sizeHintForColumn(0)
                            + lst.verticalScrollBar().sizeHint().width() + 24)
        wa = QWidgetAction(self._menu)
        wa.setDefaultWidget(lst)
        self._menu.addAction(wa)
        self._item_list = lst

    def _prep_item_list(self):
        """On each open: re-read the theme colours (a switch since the last
        rebuild would leave stale ones) and scroll to the current item."""
        lst = self._item_list
        if lst is None:
            return
        lst.setStyleSheet(_item_list_qss())
        if 0 <= lst.current_row < lst.count():
            lst.scrollToItem(lst.item(lst.current_row),
                             QListWidget.ScrollHint.PositionAtCenter)

    def _add_actions(self, menu, actions):
        """Append pinned action entries to *menu*. Each entry is (label, cb) or
        (label, cb, opts) where cb is a callable, a list of nested entries (→ a
        submenu, nested arbitrarily deep), or None (a plain/disabled row).
        *opts* (optional dict) may set checkable/checked/group/separator_after -
        see set_actions()."""
        groups: dict = {}   # group id → QActionGroup (per this menu level)
        for entry in actions:
            label, cb = entry[0], entry[1]
            opts = entry[2] if len(entry) > 2 else {}
            if isinstance(cb, list):
                # If any child entry wants to keep the menu open on click, back
                # the submenu with a stay-open QMenu so toggling doesn't dismiss.
                if any(len(e) > 2 and e[2].get("keep_open") for e in cb):
                    sub = _StayOpenMenu(label, menu)
                    menu.addMenu(sub)
                else:
                    sub = menu.addMenu(label)
                self._add_actions(sub, cb)
            elif cb is not None:
                a = menu.addAction(label)
                stateful = bool(opts.get("checkable") or opts.get("group"))
                if opts.get("checkable"):
                    a.setCheckable(True)
                    a.setChecked(bool(opts.get("checked")))
                if "value" in opts:
                    # Stash the entry's underlying value on the action so callers
                    # can find the sibling matching a given value (e.g. to revert
                    # a radio group after a blocked change).
                    a.setData(opts["value"])
                gid = opts.get("group")
                if gid is not None:
                    grp = groups.get(gid)
                    if grp is None:
                        grp = QActionGroup(menu)
                        grp.setExclusive(True)
                        groups[gid] = grp
                    grp.addAction(a)
                if stateful:
                    # `checked` is the action's post-toggle state; hand it to the
                    # cb so toggle/radio callbacks receive the value to apply.
                    # keep_open entries also get their QAction so the callback can
                    # revert the checkbox/radio in place (the menu stays open).
                    if opts.get("keep_open"):
                        a.triggered.connect(
                            lambda checked=False, c=cb, act=a: c(checked, act))
                    else:
                        a.triggered.connect(lambda checked=False, c=cb: c(checked))
                else:
                    # Plain action items keep the historical zero-arg contract.
                    a.triggered.connect(lambda _=False, c=cb: c())
            else:
                a = menu.addAction(label)
                a.setEnabled(False)
            if opts.get("separator_after"):
                menu.addSeparator()

    def paintEvent(self, event):
        # Let the base class paint the (centred) text + chrome, then draw the
        # current item's icon ourselves just LEFT of the centred label - the
        # label stays centred (QToolButton's own icon slot would left-align
        # the icon+text group together instead). Anchoring to the text (not
        # the button edge) keeps the icon clear of the rounded corner, and it
        # is skipped when a long label leaves no room, instead of clipping.
        super().paintEvent(event)
        if self._icon is not None or self._icon_only:
            return      # icon-only modes: Qt already painted the icon itself
        face = self._item_icons.get(self._current)
        if face is None:
            return
        from PySide6.QtGui import QPainter
        from PySide6.QtCore import QRect
        px = self._item_icon_px
        # 28px = the split arrow section (QSS padding); the label centres in
        # the remaining body, so mirror that when locating its left edge.
        body_w = self.width() - (28 if self.property("split") else 0)
        text_w = self.fontMetrics().horizontalAdvance(self.text())
        x = (body_w - text_w) // 2 - px - 6
        if x < 4:
            return   # no room - draw nothing rather than a clipped icon
        y = (self.height() - px) // 2
        p = QPainter(self)
        face.paint(p, QRect(x, y, px, px))
        p.end()

    def _choose(self, label):
        if label != self._current:
            self._current = label
            self._rebuild()
            if self._on_select:
                self._on_select(label)
