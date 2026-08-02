"""FlowLayout — a QLayout that arranges its items left-to-right and wraps to a
new row when the next item would overflow the available width.

Used for the footer/toolbar button rows so that longer translated labels (German
and French run 30-50% wider than English) reflow onto a second line instead of
clipping or pushing the row past the panel edge. When everything fits on one
line it looks identical to a plain QHBoxLayout.

Port of Qt's canonical FlowLayout example (height-for-width), adapted to honour
each item's own margins and a configurable spacing.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, QMargins, QPoint, QRect, QSize
from PySide6.QtWidgets import (
    QLayout, QLayoutItem, QSizePolicy, QSpacerItem, QWidget,
)


def enable_height_for_width(w: QWidget) -> None:
    """Make a FlowLayout host report its height at its ACTUAL width — Qt
    otherwise evaluates the hint at minimum width, i.e. fully wrapped."""
    pol = w.sizePolicy()
    pol.setHeightForWidth(True)
    w.setSizePolicy(pol)


class FlowLayout(QLayout):
    def __init__(self, parent: QWidget | None = None,
                 margin: int = 0, spacing: int = 4,
                 center: bool = False) -> None:
        super().__init__(parent)
        self._items: list[QLayoutItem] = []
        self._spacing = spacing
        self._center = center
        if parent is not None:
            self.setContentsMargins(margin, margin, margin, margin)

    # ---- QLayout plumbing -------------------------------------------------
    def addItem(self, item: QLayoutItem) -> None:      # noqa: N802
        self._items.append(item)

    def count(self) -> int:
        return len(self._items)

    def itemAt(self, index: int) -> QLayoutItem | None:   # noqa: N802
        if 0 <= index < len(self._items):
            return self._items[index]
        return None

    def takeAt(self, index: int) -> QLayoutItem | None:   # noqa: N802
        if 0 <= index < len(self._items):
            return self._items.pop(index)
        return None

    def expandingDirections(self) -> Qt.Orientations:     # noqa: N802
        return Qt.Orientation(0)

    def setSpacing(self, spacing: int) -> None:           # noqa: N802
        self._spacing = spacing

    def spacing(self) -> int:
        return self._spacing

    # ---- stretch ----------------------------------------------------------
    def add_stretch(self) -> None:
        """QBoxLayout.addStretch equivalent: a spring that absorbs the leftover
        width of its row, splitting the row into a left and a right group. When
        the row wraps (nothing left over) it collapses to zero, so narrow
        widths degrade to plain left-aligned flow rows."""
        self.addItem(QSpacerItem(0, 0, QSizePolicy.Expanding,
                                 QSizePolicy.Minimum))

    @staticmethod
    def _is_spring(item: QLayoutItem) -> bool:
        return (item.spacerItem() is not None
                and bool(item.expandingDirections() & Qt.Horizontal))

    @staticmethod
    def _can_grow_v(item: QLayoutItem) -> bool:
        """Whether the item's widget may be stretched past its hint height
        (vertical size policy carries the grow flag — e.g. labels), mirroring
        qSmartMaxSize in QBoxLayout."""
        w = item.widget()
        if w is None:
            return False
        return bool(w.sizePolicy().verticalPolicy().value
                    & QSizePolicy.GrowFlag.value)

    # ---- height-for-width -------------------------------------------------
    def hasHeightForWidth(self) -> bool:                  # noqa: N802
        return True

    def heightForWidth(self, width: int) -> int:          # noqa: N802
        return self._do_layout(QRect(0, 0, width, 0), test_only=True)

    def setGeometry(self, rect: QRect) -> None:           # noqa: N802
        super().setGeometry(rect)
        self._do_layout(rect, test_only=False)

    def sizeHint(self) -> QSize:                          # noqa: N802
        return self.minimumSize()

    def minimumSize(self) -> QSize:                       # noqa: N802
        size = QSize()
        for item in self._items:
            size = size.expandedTo(item.minimumSize())
        m: QMargins = self.contentsMargins()
        size += QSize(m.left() + m.right(), m.top() + m.bottom())
        return size

    # ---- core reflow ------------------------------------------------------
    def _do_layout(self, rect: QRect, test_only: bool) -> int:
        m: QMargins = self.contentsMargins()
        effective = rect.adjusted(m.left(), m.top(), -m.right(), -m.bottom())
        y = effective.y()

        # Clamp every item to the row width: an oversized label would otherwise
        # be placed at its full sizeHint and clip instead of eliding.
        avail_w = max(0, effective.width())

        def item_w(it: QLayoutItem) -> int:
            return min(it.sizeHint().width(), avail_w)

        # Pass 1: group items into rows at the available width.
        rows: list[list[QLayoutItem]] = []
        row: list[QLayoutItem] = []
        row_w = 0
        for item in self._items:
            w = item_w(item)
            needed = row_w + (self._spacing if row else 0) + w
            if row and effective.x() + needed > effective.right():
                rows.append(row)
                row, row_w = [], 0
                needed = w
            row.append(item)
            row_w = needed
        if row:
            rows.append(row)

        # Pass 2: place each row, optionally centred in the effective rect.
        # Spring items (add_stretch) share the row's leftover width between the
        # widgets on either side of them; a row with springs is never centred.
        for row in rows:
            row_w = sum(item_w(it) for it in row) \
                + self._spacing * (len(row) - 1)
            springs = sum(1 for it in row if self._is_spring(it))
            per_spring = (max(0, effective.width() - row_w) // springs
                          if springs else 0)
            x = effective.x()
            if self._center and not springs:
                x += max(0, (effective.width() - row_w) // 2)
            line_height = max((it.sizeHint().height() for it in row
                               if not self._is_spring(it)), default=0)
            for item in row:
                if self._is_spring(item):
                    x += per_spring
                    continue
                w = item.sizeHint()
                iw = min(w.width(), avail_w)
                # Match QBoxLayout's cross-axis behaviour: widgets whose
                # vertical policy can grow fill the row height; fixed-height
                # ones (buttons, line edits) are centred on the row instead of
                # top-aligned, so mixed-height rows keep a common midline.
                h = line_height if self._can_grow_v(item) else w.height()
                if not test_only:
                    item.setGeometry(QRect(
                        QPoint(x, y + (line_height - h) // 2),
                        QSize(iw, h)))
                x += iw + self._spacing
            y += line_height + self._spacing
        if rows:
            y -= self._spacing

        return y - rect.y() + m.bottom()
