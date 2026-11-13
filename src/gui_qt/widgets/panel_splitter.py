from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import QApplication, QSizePolicy, QSplitter, QSplitterHandle

from gui_qt.appearance.theme import _c, bind_theme, contrast_text


class PanelSplitter(QSplitter):
    DEFAULT_SIZES = [620, 480]

    def __init__(self, left, right, parent=None):
        super().__init__(Qt.Horizontal, parent)
        self._expanded_sizes = self.DEFAULT_SIZES[:]
        self.setObjectName("MainPanelSplit")
        self.setHandleWidth(8)
        for pane in (left, right):
            # Content hints must not push the divider outside the window.
            pane.setMinimumWidth(0)
            policy = pane.sizePolicy()
            policy.setHorizontalPolicy(QSizePolicy.Ignored)
            pane.setSizePolicy(policy)
            self.addWidget(pane)
        self.setStretchFactor(0, 5)
        self.setStretchFactor(1, 4)
        self.splitterMoved.connect(self._sync_handle)

    def createHandle(self):
        return _PanelHandle(self)

    def setSizes(self, sizes):
        if len(sizes) != 2 or any(s < 0 for s in sizes) or not sum(sizes):
            sizes = self.DEFAULT_SIZES
        total = sum(sizes)
        super().setSizes([max(1, s * 10000 // total) if s else 0 for s in sizes])
        self._sync_handle()

    def restore_panels(self):
        self.setSizes(self._expanded_sizes)
        self.splitterMoved.emit(self.handle(1).x(), 1)

    def reset_panels(self):
        self.setSizes(self.DEFAULT_SIZES)
        self.splitterMoved.emit(self.handle(1).x(), 1)

    def _sync_handle(self, *_):
        sizes = self.sizes()
        if len(sizes) == 2 and min(sizes) >= max(1, sum(sizes) // 5):
            self._expanded_sizes = sizes
        width = 18 if len(sizes) == 2 and sum(sizes) and 0 in sizes else 8
        if self.handleWidth() != width:
            self.setHandleWidth(width)
        handle = self.handle(1)
        if handle is not None:
            handle.sync_state()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._sync_handle()


class _PanelHandle(QSplitterHandle):
    def __init__(self, splitter):
        super().__init__(Qt.Horizontal, splitter)
        self._press_pos = None
        self._dragged = False
        self._collapsed = None
        self.setFocusPolicy(Qt.StrongFocus)
        bind_theme(self, roles={"BG_HEADER", "BORDER", "TEXT_MAIN", "ACCENT"})

    def refresh_theme(self, palette):
        self._colors = {key: QColor(_c(palette, key))
                        for key in ("BG_HEADER", "BORDER", "TEXT_MAIN", "ACCENT")}
        self.update()

    def sync_state(self):
        sizes = self.splitter().sizes()
        self._collapsed = (sizes.index(0) if len(sizes) == 2
                           and sum(sizes) and 0 in sizes else None)
        if self._collapsed is None:
            tip = self.tr("Drag to resize panels. Double-click to reset the layout.")
            name = self.tr("Resize modlist and plugins panels")
        else:
            name = (self.tr("Show Modlist") if self._collapsed == 0
                    else self.tr("Show Plugins"))
            tip = name + "\n" + self.tr("Click to reopen, or drag to resize.")
        self.setToolTip(tip)
        self.setAccessibleName(name)
        self.update()

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._press_pos = event.globalPosition().toPoint()
            self._dragged = False
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._press_pos is not None:
            distance = (event.globalPosition().toPoint() - self._press_pos).manhattanLength()
            self._dragged |= distance >= QApplication.startDragDistance()
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        reopen = (event.button() == Qt.LeftButton and self._press_pos is not None
                  and not self._dragged and self.rect().contains(event.position().toPoint())
                  and self._collapsed is not None)
        self._press_pos = None
        super().mouseReleaseEvent(event)
        if reopen:
            self.splitter().restore_panels()

    def mouseDoubleClickEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._press_pos = None
            self.splitter().reset_panels()
            event.accept()
        else:
            super().mouseDoubleClickEvent(event)

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key_Return, Qt.Key_Enter, Qt.Key_Space):
            self.splitter().restore_panels()
            event.accept()
        elif event.key() == Qt.Key_Home:
            self.splitter().reset_panels()
            event.accept()
        else:
            super().keyPressEvent(event)

    def paintEvent(self, event):
        if self._collapsed is None:
            super().paintEvent(event)
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        bg = self._colors["ACCENT"]
        fg = QColor(contrast_text(bg.name()))
        painter.fillRect(self.rect(), bg)
        painter.setPen(self._colors["BORDER"])
        painter.drawLine(0, 0, 0, self.height())
        painter.drawLine(self.width() - 1, 0, self.width() - 1, self.height())
        x, y = self.width() / 2, self.height() / 2
        painter.setPen(QPen(fg, 2, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        label = self.accessibleName()
        label_width = self.fontMetrics().horizontalAdvance(label)
        show_label = self.height() > label_width + 70
        arrow_y = y - label_width / 2 - 18 if show_label else y
        direction = 1 if self._collapsed == 0 else -1
        if self.layoutDirection() == Qt.RightToLeft:
            direction *= -1
        painter.drawLine(QPointF(x - direction * 2, arrow_y - 4),
                         QPointF(x + direction * 2, arrow_y))
        painter.drawLine(QPointF(x + direction * 2, arrow_y),
                         QPointF(x - direction * 2, arrow_y + 4))
        if show_label:
            painter.translate(x, y)
            painter.rotate(-90)
            painter.drawText(QRectF(-label_width / 2 - 2, -self.width() / 2,
                                   label_width + 4, self.width()), Qt.AlignCenter, label)
