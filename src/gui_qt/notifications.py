"""Progress popup + transient notification toasts for the Qt UI.

Mirrors the Tk app's deploy/restore feedback:
  * `ProgressPopup` - a small bottom-right card with a title, phase label, a
    determinate (done/total) or indeterminate (animated) bar. Reused/updated via
    `set_progress(done, total, phase)` and dismissed via `clear()`.
  * `NotificationManager.notify(text, state)` - a stacked toast (info/success/
    warning/error) that auto-dismisses after a few seconds.

Both anchor to a host window and reposition with it. All methods must be called
on the UI thread (deploy/restore workers marshal via Qt signals).
"""

from __future__ import annotations

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QProgressBar, QFrame,
)

from gui_qt.theme_qt import active_palette, _c


def _pal():
    return active_palette()


class ProgressPopup(QFrame):
    """A bottom-right progress card. One instance is reused per host; create via
    the host's NotificationHost (or directly) and drive with set_progress()."""

    WIDTH = 420

    def __init__(self, host: QWidget):
        super().__init__(host)
        self._host = host
        self.setObjectName("ProgressPopup")
        self.setFixedWidth(self.WIDTH)
        self.setAttribute(Qt.WA_StyledBackground, True)
        self._stack_offset = 0    # extra bottom margin when stacked (ProgressStack)

        v = QVBoxLayout(self)
        v.setContentsMargins(20, 18, 20, 18)
        v.setSpacing(10)

        self._title = QLabel(self.tr("Deploying"))
        self._title.setStyleSheet("font-size:18px; font-weight:600;")
        v.addWidget(self._title)

        self._phase = QLabel(self.tr("Working…"))
        self._phase.setStyleSheet(f"color:{_c(_pal(),'TEXT_DIM')}; font-size:14px;")
        self._phase.setWordWrap(True)
        v.addWidget(self._phase)

        self._bar = QProgressBar()
        self._bar.setTextVisible(False)
        self._bar.setFixedHeight(12)
        v.addWidget(self._bar)

        self._count = QLabel("")
        self._count.setStyleSheet(f"color:{_c(_pal(),'TEXT_DIM')}; font-size:13px;")
        self._count.setAlignment(Qt.AlignRight)
        v.addWidget(self._count)

        self.hide()
        host.installEventFilter(self)

    def set_progress(self, done: int, total: int, phase: str | None = None,
                     title: str | None = None, bytes_mode: bool = False):
        """*bytes_mode* formats the counter as human-readable file sizes
        ("12.3 MB / 340.0 MB") instead of raw counts - used for downloads."""
        if title:
            self._title.setText(title)
        if phase is not None:
            self._phase.setText(phase or self.tr("Working…"))
        if total > 0:
            # QProgressBar is int32 - summed byte totals (e.g. two 1.1 GB
            # downloads) overflow it. Scale the bar values down to fit; the
            # counter label below still shows the real numbers.
            bar_done, bar_total = min(done, total), total
            while bar_total > 0x7FFFFFFF:
                bar_done >>= 10
                bar_total >>= 10
            self._bar.setRange(0, bar_total)
            self._bar.setValue(bar_done)
            if bytes_mode:
                from Utils.cache_tools import format_size
                self._count.setText(self.tr("{0} / {1}").format(
                    format_size(min(done, total)), format_size(total)))
            else:
                self._count.setText(self.tr("{0} / {1}").format(done, total))
        else:
            # Indeterminate (busy) - Qt animates a range of 0,0.
            self._bar.setRange(0, 0)
            self._count.setText("")
        if not self.isVisible():
            self.show()
        self._reposition()
        self.raise_()

    def clear(self):
        self.hide()

    def _reposition(self):
        self.adjustSize()
        self.setFixedWidth(self.WIDTH)
        m = 16
        x = self._host.width() - self.width() - m
        y = self._host.height() - self.height() - m - self._stack_offset
        self.move(max(0, x), max(0, y))

    def eventFilter(self, obj, event):
        from PySide6.QtCore import QEvent
        if obj is self._host and event.type() in (QEvent.Resize, QEvent.Move) \
                and self.isVisible():
            self._reposition()
        return super().eventFilter(obj, event)


class ProgressStack:
    """Keyed progress cards stacked in the host's bottom-right corner.

    One card per concurrent operation: key "op" is the shared install/deploy/
    restore card; downloads get a unique key each so a download's progress no
    longer clobbers (or, on finish, hides) the install card. Cards are created
    on first set_progress(key=...) and destroyed by clear(key=...).
    """

    def __init__(self, host: QWidget):
        self._host = host
        self._popups: dict[str, ProgressPopup] = {}   # insertion order = stack order

    def set_progress(self, done: int, total: int, phase: str | None = None,
                     title: str | None = None, bytes_mode: bool = False,
                     key: str = "op"):
        p = self._popups.get(key)
        if p is None:
            p = ProgressPopup(self._host)
            self._popups[key] = p
        p.set_progress(done, total, phase, title=title, bytes_mode=bytes_mode)
        self._restack()

    def clear(self, key: str = "op"):
        p = self._popups.pop(key, None)
        if p is not None:
            p.hide()
            p.deleteLater()
        self._restack()

    def clear_all(self):
        for key in list(self._popups):
            self.clear(key)

    def _restack(self):
        """Bottom-most card sits at the corner; later cards stack upward."""
        offset = 0
        for p in self._popups.values():
            if not p.isVisible():
                continue
            p._stack_offset = offset
            p._reposition()
            offset += p.height() + 8


class _Toast(QFrame):
    """A single notification card. Auto-dismisses after a few seconds unless
    *sticky* is set, in which case it lingers until dismissed programmatically
    (via the handle returned by NotificationManager.notify)."""

    def __init__(self, manager: "NotificationManager", text: str, state: str,
                 sticky: bool = False):
        super().__init__(manager._host)
        self._manager = manager
        self.setObjectName("Toast")
        self.setProperty("state", state)      # info/success/warning/error
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setMinimumWidth(340)
        self.setMaximumWidth(460)

        h = QHBoxLayout(self)
        h.setContentsMargins(18, 14, 18, 14)
        h.setSpacing(12)
        dot = QLabel("●")
        dot.setObjectName("ToastDot")
        dot.setProperty("state", state)
        dot.setStyleSheet("font-size:16px;")
        h.addWidget(dot)
        self._label = QLabel(text)
        self._label.setWordWrap(True)
        self._label.setStyleSheet("font-size:14px;")
        h.addWidget(self._label, 1)

        self.adjustSize()
        if not sticky:
            # Auto-dismiss (errors/warnings linger a little longer).
            ms = 5000 if state in ("warning", "error") else 3200
            QTimer.singleShot(ms, self._dismiss)

    def _dismiss(self):
        self._manager._remove(self)


class ToastHandle:
    """Returned by NotificationManager.notify(sticky=True) so the caller can
    update the text or dismiss the toast once its long-running task finishes."""

    def __init__(self, manager: "NotificationManager", toast: "_Toast"):
        self._manager = manager
        self._toast = toast

    def set_text(self, text: str):
        t = self._toast
        if t is not None and t in self._manager._toasts:
            t._label.setText(text)
            self._manager._restack()

    def dismiss(self, text: str | None = None, state: str | None = None,
                auto_dismiss_ms: int = 0):
        """Remove the sticky toast. If *text* is given, first swap it to a
        transient toast (optionally re-styled via *state*) that auto-dismisses
        after *auto_dismiss_ms* - handy for turning "Checking…" into a final
        "Found N updates" that then fades on its own."""
        t = self._toast
        if t is None or t not in self._manager._toasts:
            return
        if text is None:
            self._manager._remove(t)
        else:
            if state is not None:
                t.setProperty("state", state)
                t.style().unpolish(t)
                t.style().polish(t)
            t._label.setText(text)
            # Morphed final text never passes through MainWindow._notify, so
            # record it here for the notification-history dropdown.
            if callable(getattr(self._manager, "on_record", None)):
                self._manager.on_record(text, state or t.property("state"))
            self._manager._restack()
            ms = auto_dismiss_ms or 3200
            QTimer.singleShot(ms, t._dismiss)
        self._toast = None


class NotificationManager:
    """Stacks transient toasts in the host's top-right corner."""

    def __init__(self, host: QWidget):
        self._host = host
        self._toasts: list[_Toast] = []
        self.on_record = None   # optional (text, state) hook for history
        host.installEventFilter(self._Filter(self))

    class _Filter(QWidget):
        def __init__(self, mgr):
            super().__init__()
            self._mgr = mgr

        def eventFilter(self, obj, event):
            from PySide6.QtCore import QEvent
            if event.type() in (QEvent.Resize, QEvent.Move):
                self._mgr._restack()
            return False

    def notify(self, text: str, state: str = "info", sticky: bool = False):
        """Show a toast. Transient by default; pass *sticky*=True to keep it on
        screen until the returned ToastHandle is dismissed."""
        t = _Toast(self, text, state, sticky=sticky)
        self._toasts.append(t)
        t.show()
        self._restack()
        if sticky:
            return ToastHandle(self, t)
        return None

    def _remove(self, toast: _Toast):
        if toast in self._toasts:
            self._toasts.remove(toast)
            toast.deleteLater()
            self._restack()

    def _restack(self):
        m = 16
        y = m
        for t in self._toasts:
            t.adjustSize()
            x = self._host.width() - t.width() - m
            t.move(max(0, x), y)
            t.raise_()
            y += t.height() + 8
