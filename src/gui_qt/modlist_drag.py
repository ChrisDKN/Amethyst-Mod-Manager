"""Mouse dragging between two presentations of the same modlist."""

from PySide6.QtCore import QObject, QEvent, Qt
from PySide6.QtWidgets import QApplication


class ModListDrag(QObject):
    def __init__(self, views, parent):
        super().__init__(parent)
        self.views = views
        self.source = None
        self.target = None
        self.names = set()
        self._ending = False
        for view in views:
            view.modlist_drag = self
        QApplication.instance().installEventFilter(self)

    def eventFilter(self, watched, event):
        if self.source is not None:
            if (event.type() == QEvent.ApplicationDeactivate
                    or (event.type() == QEvent.KeyPress and event.key() == Qt.Key_Escape)):
                self.cancel()
                return event.type() == QEvent.KeyPress
        return False

    def move(self, source, global_pos):
        if self.source is None:
            self.source = source
            self.names = {source.model().entry(r).name for r in source._drag_rows}
        widget = QApplication.widgetAt(global_pos)
        target = next((view for view in self.views
                       if view.isVisible() and view.isEnabled()
                       and (widget is view.viewport()
                            or (widget is not None and view.viewport().isAncestorOf(widget)))
                       and not view.model().flat_sort_active), None)
        if self.target is not target:
            self._clear_target()
            self.target = target
        if target is None:
            return
        target._drag_rows = [r for r, e in enumerate(target.model()._entries)
                             if e.name in self.names]
        if len(target._drag_rows) != len(self.names):
            self.cancel()
            return
        target._drag_active = True
        target._sync_group_end_markers()
        target._last_mouse_y = target.viewport().mapFromGlobal(global_pos).y()
        target._update_drop_slot(target._last_mouse_y)
        target._scroll_timer.start()
        target.viewport().update()

    def _clear_target(self):
        target = self.target
        if target is None:
            return
        target._scroll_timer.stop()
        target._drop_slot = -1
        target._drop_collapsed_sep = -1
        target._drop_group = None
        target._drop_group_end = None
        if target is not self.source:
            target._drag_active = False
            target._drag_rows = []
        target._sync_group_end_markers()
        target.viewport().update()

    def finish(self, global_pos):
        source = self.source
        if source is None:
            return
        self.move(source, global_pos)
        target = self.target
        positions = [(view, view.capture_position()) for view in self.views]
        try:
            if target is not None:
                target._commit_drop()
        finally:
            self.cancel()
            for view, position in positions:
                view.restore_position(position)

    def cancel(self):
        if self._ending:
            return
        self._ending = True
        try:
            self.source = self.target = None
            self.names.clear()
            for view in self.views:
                view._end_drag()
        finally:
            self._ending = False

    def dispose(self):
        self.cancel()
        QApplication.instance().removeEventFilter(self)
        for view in self.views:
            view.modlist_drag = None
        self.views = []
