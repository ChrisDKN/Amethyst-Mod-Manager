"""Detachable tab widget - the Qt replacement for the Tk overlays.

Views that the Tk app showed as overlays (Add Game, Nexus browser, settings,
wizards, …) open here as TABS instead of new windows. A tab can be:
  * switched between (multiple "overlays" open at once),
  * closed,
  * dragged out of the tab bar → becomes a floating window,
  * closed → redocks back as a tab (close == redock).
Programmatic close_tab(key) dismisses the view for real (no redock).

State is preserved across detach/reattach because the page widget is reparented,
never recreated.

Usage:
    tabs = DetachableTabWidget()
    tabs.add_permanent(mods_widget, "Mods")      # tab 0, not closable
    tabs.open_tab(add_game_widget, "Add game", key="add_game")  # focus if exists
"""

from __future__ import annotations

from PySide6.QtCore import Qt, QPoint, Signal, QCoreApplication
from PySide6.QtWidgets import (
    QTabWidget, QTabBar, QWidget, QMainWindow, QStackedWidget,
    QMenu,
)

# The three ways a view can be presented (see DetachableTabWidget docstring).
MODE_FULL = "full"          # a normal full-UI tab (like the Nexus browser)
MODE_MODLIST = "modlist"    # panel-scoped: takes over the modlist panel only
MODE_PLUGINS = "plugins"    # panel-scoped: takes over the plugins panel only


class _DetachTabBar(QTabBar):
    """Tab bar that emits detach_requested when a tab is dragged far enough
    outside the bar (the gesture people get subtly wrong - so it's centralised
    here with a clear vertical-distance threshold)."""

    detach_requested = Signal(int, QPoint)   # (tab index, global drop pos)
    pin_requested = Signal(int, QPoint)      # (tab index, global menu pos)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMovable(True)               # reorder within the bar
        self._press_index = -1

    def contextMenuEvent(self, event):
        idx = self.tabAt(event.pos())
        if idx != -1:
            self.pin_requested.emit(idx, event.globalPos())

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._press_index = self.tabAt(event.position().toPoint())
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        # Detach when the pointer leaves the bar vertically by a clear margin
        # (so ordinary horizontal reordering still works).
        if self._press_index != -1:
            off = event.position().toPoint()
            if off.y() < -24 or off.y() > self.height() + 24:
                idx = self._press_index
                self._press_index = -1
                self.detach_requested.emit(idx, event.globalPosition().toPoint())
                return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        self._press_index = -1
        super().mouseReleaseEvent(event)


class _FloatingTab(QMainWindow):
    """A detached tab living in its own window. Closing the window REDOCKS the
    view back into the tab bar (close == redock). The page widget is reparented,
    never recreated."""

    # Emitted when the window should give its page back to the tab widget.
    redock_requested = Signal(QWidget, str)   # (page, title)

    def __init__(self, page: QWidget, title: str, parent=None):
        super().__init__(parent)
        self._page = page
        self._title = title
        self._redocking = False
        self.setAttribute(Qt.WA_DeleteOnClose)
        self.setWindowTitle(title)
        self.resize(max(900, page.width()), max(600, page.height()))
        self.setCentralWidget(page)
        page.show()                            # was hidden by removeTab → show it
        # A detached tab is its own window, so it needs its own scanline sheet
        # (no-op unless the active theme asks for one).
        try:
            from gui_qt.appearance.scanline_overlay import attach as attach_scanlines
            attach_scanlines(self)
        except Exception:
            pass

    def take_page(self) -> QWidget | None:
        """Release the page without deleting it (for redocking)."""
        self._redocking = True
        page = self.centralWidget()
        if page is not None:
            page.hide()
            # Keep its parent until insertion: takeCentralWidget reparents to
            # None, forcing an extra stylesheet pass through the whole view.
            self.layout().removeWidget(page)
        return page

    def closeEvent(self, event):
        # Redock unless the page was already taken by the manager.
        if not self._redocking:
            page = self.take_page()
            if page is not None:
                self.redock_requested.emit(page, self._title)
        super().closeEvent(event)


class DetachableTabWidget(QTabWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._bar = _DetachTabBar(self)
        self.setTabBar(self._bar)
        # Closing the current tab returns to the PREVIOUSLY ACTIVE tab (Qt keeps
        # a per-tab back-pointer, adjusted across reorders/removals) - not the
        # neighbour on the right.
        self._bar.setSelectionBehaviorOnRemove(
            QTabBar.SelectionBehavior.SelectPreviousTab)
        self.setTabsClosable(True)
        self.setMovable(True)
        self.setDocumentMode(True)
        self._bar.setDrawBase(False)        # no baseline frame under the tabs
        self._bar.setExpanding(False)       # tabs hug their text (left-aligned)
        self._permanent: set[int] = set()      # indices that can't be closed
        self._keys: dict[str, QWidget] = {}     # key → page (focus-if-open)
        self._floats: list[_FloatingTab] = []
        # Panel-scoped tabs: a tab in this bar that, instead of a full-UI page,
        # takes over JUST one panel (a target QStackedWidget). id(placeholder) →
        # (target_stack, scoped_widget, scoped_page_index). When such a tab is
        # selected we keep the permanent full-UI page visible and flip the
        # target stack to the scoped page; deselecting restores the stack to 0.
        self._scoped: dict[int, tuple] = {}
        self._permanent_widget: QWidget | None = None
        # Per-panel active scoped tab: id(target_stack) → id(placeholder).
        # Lets a modlist-scoped AND a plugins-scoped tab show at the same time -
        # selecting one only claims ITS panel; the other panel keeps whatever
        # scoped view it was showing. Cleared (all panels reset to page 0) when
        # a full-UI or the permanent tab is selected.
        self._panel_active: dict[int, int] = {}
        # Panel stacks a view can be re-pinned into, and each open view's current
        # presentation mode (keyed by id(content-widget)). The content widget is
        # the REAL view for both full and scoped tabs (for scoped tabs that's the
        # widget in the panel stack, NOT the empty bar placeholder).
        self._modlist_stack: QStackedWidget | None = None
        self._plugins_stack: QStackedWidget | None = None
        self._modes: dict[int, str] = {}

        self.tabCloseRequested.connect(self._on_close_requested)
        self._bar.detach_requested.connect(self._detach)
        self._bar.pin_requested.connect(self._on_pin_requested)
        self.currentChanged.connect(self._on_current_changed)

    def register_scope_targets(self, modlist_stack: QStackedWidget,
                               plugins_stack: QStackedWidget):
        """Tell the widget which panel stacks the 'Pin to Modlist/Plugins' menu
        should target. Call once after the panels are built."""
        self._modlist_stack = modlist_stack
        self._plugins_stack = plugins_stack

    def _update_bar_visibility(self):
        self.tabBar().setVisible(self.count() > 1)

    def _on_current_changed(self, index: int):
        self._update_bar_visibility()
        w = self.widget(index)
        scoped = self._scoped.get(id(w)) if w is not None else None
        if scoped is not None:
            # A panel-scoped tab was selected. Its own page is an empty
            # placeholder - we don't want to show that. Instead snap the content
            # back to the permanent full-UI page and show the scoped widget in
            # its target panel stack, so the full UI stays live with that panel
            # showing the scoped view. Only THIS tab's panel is claimed: a scoped
            # tab already showing in the OTHER panel stays visible, so a modlist
            # tab and a plugins tab can be on screen together. The tab stays
            # HIGHLIGHTED via the bar's own current index (we leave the bar on
            # the scoped tab; only the displayed page is forced to the permanent
            # one).
            target_stack, _scoped_widget, _page_idx = scoped
            self._panel_active[id(target_stack)] = id(w)
            self._sync_scoped_stacks()
            self._show_permanent_page_keeping_tab(index)
        else:
            # Full-UI or permanent tab selected → hide ALL scoped panels.
            self._panel_active.clear()
            self._sync_scoped_stacks()

    def is_full_tab_active(self) -> bool:
        """Whether the selected tab replaces the permanent main UI entirely."""
        w = self.widget(self.currentIndex())
        return (w is not None
                and id(w) not in self._permanent
                and id(w) not in self._scoped)

    def _sync_scoped_stacks(self):
        """Bring every panel stack in line with _panel_active: show the active
        scoped widget where one is recorded (and still open), else page 0.
        Resolves pages by WIDGET, not stored index - closing another scoped tab
        on the same stack shifts indices (removeWidget), which would make a
        stored page_idx stale."""
        stacks: dict[int, QStackedWidget] = {}
        for (ts, _s, _p) in self._scoped.values():
            stacks[id(ts)] = ts
        for sid, ts in stacks.items():
            entry = self._scoped.get(self._panel_active.get(sid))
            if entry is not None:
                ts.setCurrentWidget(entry[1])
            else:
                self._panel_active.pop(sid, None)
                ts.setCurrentIndex(0)

    def _show_permanent_page_keeping_tab(self, scoped_index: int):
        """Display the permanent full-UI page in the content area while leaving
        the tab BAR highlighting the scoped tab at *scoped_index*. Done by
        reaching into the QTabWidget's private QStackedWidget so the bar and the
        shown page can differ (Qt normally keeps them in lockstep)."""
        perm = self._permanent_widget
        if perm is None:
            return
        stack = self.findChild(QStackedWidget)
        if stack is not None:
            stack.setCurrentWidget(perm)

    # -- adding tabs --------------------------------------------------------
    def add_permanent(self, widget: QWidget, title: str) -> int:
        """Add a tab that can't be closed or detached (e.g. the Mods view)."""
        idx = self.addTab(widget, title)
        self._permanent.add(id(widget))
        self._permanent_widget = widget
        self._refresh_close_buttons()
        self._update_bar_visibility()
        return idx

    def open_scoped_tab(self, scoped_widget: QWidget, title: str,
                        target_stack, key: str | None = None):
        """Open a PANEL-SCOPED tab: it appears in the shared tab bar, but selecting
        it keeps the permanent full-UI page visible and shows *scoped_widget* in
        *target_stack* (one panel's QStackedWidget). The rest of the UI - the
        other panel, headers, footers - stays live and interactive.

        Re-opening the same *key* focuses the existing tab. Returns the tab's
        placeholder widget (the handle used as its identity)."""
        if key is not None and key in self._keys:
            self._focus(self._keys[key])
            return self._keys[key]
        # Honour a saved pin (user re-pinned this view before). A pin to a
        # different panel swaps the target stack; a pin to full screen routes to
        # open_tab instead. Falls back to the requested stack if unregistered.
        pinned = self._saved_pin(key)
        if pinned == MODE_FULL:
            return self.open_tab(scoped_widget, title, key=key)
        if pinned in (MODE_MODLIST, MODE_PLUGINS):
            alt = self._stack_for_mode(pinned)
            if alt is not None:
                target_stack = alt
        # The tab page itself is an empty placeholder - the real content lives in
        # target_stack. addTab needs a widget; this 0-size stub never shows.
        placeholder = QWidget()
        page_idx = target_stack.addWidget(scoped_widget)
        self._scoped[id(placeholder)] = (target_stack, scoped_widget, page_idx)
        idx = self.addTab(placeholder, title)
        if key is not None:
            self._keys[key] = placeholder
            placeholder.setProperty("_tab_key", key)
        self._modes[id(scoped_widget)] = self._mode_for_stack(target_stack)
        self.setCurrentIndex(idx)
        self._update_bar_visibility()
        return placeholder

    def _mode_for_stack(self, stack: QStackedWidget) -> str:
        if stack is self._plugins_stack:
            return MODE_PLUGINS
        return MODE_MODLIST

    def _stack_for_mode(self, mode: str) -> QStackedWidget | None:
        if mode == MODE_PLUGINS:
            return self._plugins_stack
        if mode == MODE_MODLIST:
            return self._modlist_stack
        return None

    def _saved_pin(self, key: "str | None") -> "str | None":
        """The persisted preferred mode for *key*, or None. Isolated so it never
        breaks tab-opening if ui_config is unavailable."""
        if not key:
            return None
        try:
            from Utils.ui.config import get_tab_pin
            return get_tab_pin(key)
        except Exception:
            return None

    def open_tab(self, widget: QWidget, title: str, key: str | None = None):
        """Open *widget* as a tab and focus it. If *key* is already open
        (tab or float), focus that instead of adding a duplicate."""
        if key is not None and key in self._keys:
            existing = self._keys[key]
            self._focus(existing)
            return existing
        # Honour a saved pin to a panel (user re-pinned this full view into a
        # panel before). Route to open_scoped_tab if that stack is registered.
        pinned = self._saved_pin(key)
        if pinned in (MODE_MODLIST, MODE_PLUGINS):
            stack = self._stack_for_mode(pinned)
            if stack is not None:
                return self.open_scoped_tab(widget, title, stack, key=key)
        idx = self.addTab(widget, title)
        if key is not None:
            self._keys[key] = widget
            widget.setProperty("_tab_key", key)
        self._modes[id(widget)] = MODE_FULL
        self.setCurrentIndex(idx)
        self._update_bar_visibility()
        return widget

    def close_tab(self, key: str):
        """Close the tab/float registered under *key* (no-op if not open)."""
        widget = self._keys.get(key)
        if widget is None:
            for flt in list(self._floats):
                if getattr(flt, "_tab_key", None) == key:
                    self._floats = [f for f in self._floats if f is not flt]
                    page = flt.take_page()      # release without redock
                    self._notify_tab_closing(page)
                    flt.close()
                    if page is not None:
                        page.deleteLater()
                    return
            return
        # Scoped tab? Route through the scoped teardown so the target panel
        # stack is reset to page 0 (a plain removeTab would strand it).
        if id(widget) in self._scoped:
            self._close_scoped(widget)
            return
        # Docked tab?
        idx = self.indexOf(widget)
        if idx != -1:
            self._notify_tab_closing(widget)
            self.removeTab(idx)
            self._forget(widget)
            self._modes.pop(id(widget), None)
            widget.deleteLater()
            self._update_bar_visibility()
            return
        # Otherwise a floating window - dismiss it for real (don't redock).
        for flt in list(self._floats):
            if flt._page is widget:
                self._floats = [f for f in self._floats if f is not flt]
                self._forget(widget)
                page = flt.take_page()      # release without redock
                self._notify_tab_closing(page)
                flt.close()
                if page is not None:
                    page.deleteLater()
                return

    # -- close / detach -----------------------------------------------------
    def _on_close_requested(self, index: int):
        w = self.widget(index)
        if id(w) in self._permanent:
            return
        # A view may veto the tab-bar ✕ (e.g. an xEdit wizard while the tool
        # is running). For scoped tabs the page is a placeholder - ask the
        # real content widget in the panel stack.
        scoped = self._scoped.get(id(w))
        content = scoped[1] if scoped is not None else w
        blocked = getattr(content, "tab_close_blocked", None)
        if blocked is not None and blocked():
            return
        if scoped is not None:
            self._close_scoped(w)
            return
        self._notify_tab_closing(content)
        self.removeTab(index)
        self._forget(w)
        self._modes.pop(id(w), None)
        w.deleteLater()
        self._update_bar_visibility()

    def _close_tabs(self, pages: list[QWidget]):
        for page in reversed(pages):
            index = self.indexOf(page)
            if index != -1:
                self._on_close_requested(index)

    def _close_scoped(self, placeholder: QWidget):
        """Tear down a panel-scoped tab: remove its widget from the target stack,
        reset that stack to page 0, and drop the tab. Selection then lands on the
        previously active tab (SelectPreviousTab), not forced to the permanent
        page."""
        target_stack, scoped_widget, _idx = self._scoped.pop(id(placeholder))
        self._notify_tab_closing(scoped_widget)
        target_stack.setCurrentIndex(0)
        target_stack.removeWidget(scoped_widget)
        self._modes.pop(id(scoped_widget), None)
        scoped_widget.deleteLater()
        # Clear BEFORE removeTab: the currentChanged it fires may activate
        # another scoped tab, and must not be clobbered afterwards.
        if self._panel_active.get(id(target_stack)) == id(placeholder):
            self._panel_active.pop(id(target_stack), None)
        tab_idx = self.indexOf(placeholder)
        if tab_idx != -1:
            self.removeTab(tab_idx)
        self._forget(placeholder)
        placeholder.deleteLater()
        # Re-sync the panel stacks for whichever tab is now current - closing a
        # NON-current scoped tab fires no currentChanged, yet we reset a stack
        # the current scoped tab may share.
        self._on_current_changed(self.currentIndex())

    # -- re-pin (move a view between full / modlist / plugins) --------------
    def _on_pin_requested(self, index: int, global_pos: QPoint):
        w = self.widget(index)
        if w is None:
            return
        # Resolve the REAL content widget + its current mode. For a scoped tab
        # the QTabWidget page is the empty placeholder; the content lives in the
        # panel stack.
        scoped = self._scoped.get(id(w))
        content = scoped[1] if scoped is not None else w
        current = self._modes.get(id(content), MODE_FULL)

        menu = QMenu(self)
        detach = menu.addAction(QCoreApplication.translate(
            "DetachableTabWidget", "Detach"))
        detach.setEnabled(id(w) not in self._permanent)
        detach.triggered.connect(
            lambda _=False: self._detach(self.indexOf(w), global_pos))
        menu.addSeparator()
        entries = [
            (MODE_MODLIST, QCoreApplication.translate(
                "DetachableTabWidget", "Pin to Modlist Panel"),
             self._modlist_stack),
            (MODE_PLUGINS, QCoreApplication.translate(
                "DetachableTabWidget", "Pin to Plugins Panel"),
             self._plugins_stack),
            (MODE_FULL, QCoreApplication.translate(
                "DetachableTabWidget", "Pin to Full Screen"), True),
        ]
        for mode, label, target in entries:
            if target is None or id(w) in self._permanent:
                continue
            act = menu.addAction(label)
            act.setCheckable(True)
            if mode == current:
                act.setChecked(True)
                act.setEnabled(False)   # already in this mode
            act.triggered.connect(
                lambda _=False, m=mode, c=content: self._repin(c, m))
        if not menu.actions()[-1].isSeparator():
            menu.addSeparator()
        close_menu = menu.addMenu(QCoreApplication.translate(
            "DetachableTabWidget", "Close"))
        closable = [self.widget(i) for i in range(self.count())
                    if id(self.widget(i)) not in self._permanent]
        close_entries = [
            (QCoreApplication.translate("DetachableTabWidget", "Close Others"),
             [page for page in closable if page is not w]),
            (QCoreApplication.translate("DetachableTabWidget", "Close All to the Right"),
             [page for page in closable if self.indexOf(page) > index]),
            (QCoreApplication.translate("DetachableTabWidget", "Close Tab"),
             [w] if id(w) not in self._permanent else []),
        ]
        for label, pages in close_entries:
            act = close_menu.addAction(label)
            act.setEnabled(bool(pages))
            act.triggered.connect(
                lambda _=False, targets=pages: self._close_tabs(targets))
        from gui_qt.runtime.plugin_menus import add_plugin_actions, menu_item
        key = next((key for key, page in self._keys.items() if page is w), "")
        add_plugin_actions(menu, self, "tabs", menu_item("tab", self.tabText(index), id=key))
        if menu.actions():
            menu.exec(global_pos)
        menu.deleteLater()

    def _repin(self, content: QWidget, target_mode: str):
        """Move an already-open view *content* into *target_mode*, preserving its
        state, key and title. Reuses the extract logic from detach/redock - the
        widget is reparented, never recreated."""
        if self._modes.get(id(content)) == target_mode:
            return

        # 1. Locate + extract the content widget from wherever it lives now,
        #    capturing (title, key). We do NOT delete it.
        title = ""
        key = content.property("_tab_key")

        # a) Currently a floating window? Redock intent → pull its page.
        flt = next((f for f in self._floats if f._page is content), None)
        if flt is not None:
            title = flt._title
            self._floats = [f for f in self._floats if f is not flt]
            content = flt.take_page() or content
            flt.close()
        else:
            # b) Currently a scoped tab? Placeholder is the bar page; content is
            #    in a panel stack.
            placeholder = None
            for pid, (ts, sw, _pi) in list(self._scoped.items()):
                if sw is content:
                    placeholder = self._widget_by_id(pid)
                    if placeholder is not None:
                        title = self.tabText(self.indexOf(placeholder))
                        # For a scoped tab the _tab_key lives on the PLACEHOLDER,
                        # not the content widget - read it there.
                        if not key:
                            key = placeholder.property("_tab_key")
                    ts.setCurrentIndex(0)
                    ts.removeWidget(content)
                    self._scoped.pop(pid, None)
                    if self._panel_active.get(id(ts)) == pid:
                        self._panel_active.pop(id(ts), None)
                    ti = self.indexOf(placeholder) if placeholder else -1
                    if ti != -1:
                        self.removeTab(ti)
                    if placeholder is not None:
                        self._forget(placeholder)
                        placeholder.deleteLater()
                    break
            else:
                # c) A plain full-UI tab.
                ti = self.indexOf(content)
                if ti != -1:
                    title = self.tabText(ti)
                    self.removeTab(ti)
        self._modes.pop(id(content), None)

        # 2. Persist the choice so this view reopens in target_mode next time.
        #    Saved BEFORE re-inserting so the open_* redirect (which reads the
        #    pin) agrees with where we're putting it, not the old value.
        if key:
            try:
                from Utils.ui.config import save_tab_pin
                save_tab_pin(key, target_mode)
            except Exception:
                pass

        # 3. Re-insert via the target mode's normal path. Clear the stale key
        #    first so the focus-if-open guard doesn't short-circuit.
        if key and key in self._keys:
            del self._keys[key]
        if target_mode == MODE_FULL:
            self.open_tab(content, title, key=key)
        else:
            stack = self._stack_for_mode(target_mode)
            content.setProperty("_tab_key", key)
            self.open_scoped_tab(content, title, stack, key=key)
        self._update_bar_visibility()

    def _widget_by_id(self, wid: int) -> QWidget | None:
        for i in range(self.count()):
            w = self.widget(i)
            if w is not None and id(w) == wid:
                return w
        return None

    def _detach(self, index: int, drop_pos: QPoint):
        w = self.widget(index)
        if w is None or id(w) in self._permanent:
            return
        title = self.tabText(index)
        key = w.property("_tab_key")
        # SCOPED tab: its QTabWidget page is an empty placeholder - the REAL
        # content lives in a target panel stack. Float that real widget instead
        # (else the detached window is blank), and remember its scope so redock
        # puts it back into the panel stack.
        scoped = self._scoped.pop(id(w), None)
        if scoped is not None:
            target_stack, scoped_widget, _pi = scoped
            if self._panel_active.get(id(target_stack)) == id(w):
                self._panel_active.pop(id(target_stack), None)
            target_stack.setCurrentIndex(0)        # reveal the panel again
            target_stack.removeWidget(scoped_widget)
            self.removeTab(index)
            self._forget(w)
            w.deleteLater()                        # drop the placeholder
            flt = _FloatingTab(scoped_widget, title, self.window())
            flt._tab_key = key
            flt._scoped_target = target_stack       # mark for re-scope on redock
            scoped_widget.setProperty("_tab_key", key)
        else:
            self.removeTab(index)
            flt = _FloatingTab(w, title, self.window())
            flt._tab_key = key
            flt._scoped_target = None
        flt.redock_requested.connect(self._on_redock)
        flt.move(drop_pos)
        flt.show()
        flt.raise_()
        flt.activateWindow()
        self._floats.append(flt)
        self._update_bar_visibility()

    def _on_redock(self, page: QWidget, title: str):
        """A floating window closed → bring its page back as a tab."""
        # Was this a SCOPED float? Re-scope it into its panel stack.
        target = None
        for f in self._floats:
            if f._page is page:
                target = getattr(f, "_scoped_target", None)
                break
        self._floats = [f for f in self._floats if f._page is not page]
        key = page.property("_tab_key")
        if target is not None:
            # Re-create the scoped tab (placeholder in the bar, widget in stack).
            placeholder = QWidget()
            page_idx = target.addWidget(page)
            self._scoped[id(placeholder)] = (target, page, page_idx)
            idx = self.addTab(placeholder, title)
            if key:
                self._keys[key] = placeholder
                placeholder.setProperty("_tab_key", key)
            self.setCurrentIndex(idx)
        else:
            idx = self.addTab(page, title)
            page.show()
            self.setCurrentIndex(idx)
        self._update_bar_visibility()

    # -- helpers ------------------------------------------------------------
    def _focus(self, widget: QWidget):
        idx = self.indexOf(widget)
        if idx != -1:
            self.setCurrentIndex(idx)
        else:
            # It's floating - raise its window.
            for f in self._floats:
                if f._page is widget:
                    f.raise_(); f.activateWindow()
                    break

    def _forget(self, widget: QWidget):
        key = widget.property("_tab_key")
        if key and key in self._keys:
            del self._keys[key]

    @staticmethod
    def _notify_tab_closing(widget: QWidget | None) -> None:
        """Give content a final cleanup hook before permanent dismissal.

        Detaching and closing a floating window merely move/redock content and
        deliberately do not call this hook.
        """
        if widget is None:
            return
        callback = getattr(widget, "tab_closing", None)
        if callable(callback):
            try:
                callback()
            except (RuntimeError, ReferenceError):
                pass

    # -- key-based helpers --------------------------------------------------
    def has_key(self, key: str) -> bool:
        if key in self._keys:
            return True
        # A detached SCOPED tab forgets its key from _keys but its float still
        # carries _tab_key - treat that as open so close_tab/re-open see it.
        return any(getattr(f, "_tab_key", None) == key for f in self._floats)

    def focus_key(self, key: str):
        w = self._keys.get(key)
        if w is not None:
            self._focus(w)

    def content_for_key(self, key: str) -> QWidget | None:
        """Return the real content widget registered under *key*.

        A panel-pinned tab stores a placeholder in ``_keys`` while its actual
        view lives in a panel stack, so callers must not use ``_keys`` directly
        when they need to refresh the view itself.
        """
        handle = self._keys.get(key)
        if handle is not None:
            scoped = self._scoped.get(id(handle))
            return scoped[1] if scoped is not None else handle
        for flt in self._floats:
            if getattr(flt, "_tab_key", None) == key:
                return getattr(flt, "_page", None)
        return None

    def set_tab_title(self, key: str, title: str):
        w = self._keys.get(key)
        if w is not None:
            idx = self.indexOf(w)
            if idx != -1:
                self.setTabText(idx, title)
            return
        # Detached: retitle the floating window instead.
        for flt in self._floats:
            if getattr(flt, "_tab_key", None) == key:
                flt._title = title
                flt.setWindowTitle(title)
                return

    def _refresh_close_buttons(self):
        """Permanent tabs show no close button."""
        for i in range(self.count()):
            if id(self.widget(i)) in self._permanent:
                self.tabBar().setTabButton(i, QTabBar.RightSide, None)
