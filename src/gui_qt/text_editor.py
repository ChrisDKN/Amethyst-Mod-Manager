"""Panel-scoped text editor for the Text Files tab.

A save-capable editor opened as a modlist-panel-scoped tab (like the image
preview): header + Find box + QPlainTextEdit + Save/Revert. Dirty-tracked (the
tab title gets a '*'); Find highlights matches with next/prev. Reuse-one-editor:
load_file() swaps the file in place. Ports the load/save/find behaviour of the Tk
IniFileEditorPanel.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, Signal, QRect, QSize, QTimer
from PySide6.QtGui import (
    QTextCursor, QTextCharFormat, QColor, QFont, QPainter, QShortcut,
    QKeySequence,
)
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPlainTextEdit, QLineEdit,
    QPushButton, QTextEdit,
)

from gui_qt.theme_qt import active_palette, bind_theme, bind_theme_icon, _c, qc


class _LineNumberArea(QWidget):
    """The gutter widget painted by the code editor (standard Qt pattern)."""

    def __init__(self, editor):
        super().__init__(editor)
        self._editor = editor

    def sizeHint(self):
        return QSize(self._editor.line_number_area_width(), 0)

    def paintEvent(self, event):
        self._editor.paint_line_numbers(event)


class _CodeEditor(QPlainTextEdit):
    """QPlainTextEdit with a left line-number gutter."""

    def __init__(self, gutter_bg, gutter_fg, parent=None):
        super().__init__(parent)
        self._gutter_bg = QColor(gutter_bg)
        self._gutter_fg = QColor(gutter_fg)
        self._gutter = _LineNumberArea(self)
        self.blockCountChanged.connect(lambda _n: self._update_gutter_width())
        self.updateRequest.connect(self._update_gutter)
        self._update_gutter_width()

    def line_number_area_width(self) -> int:
        digits = max(2, len(str(max(1, self.blockCount()))))
        return 12 + self.fontMetrics().horizontalAdvance("9") * digits

    def _update_gutter_width(self):
        self.setViewportMargins(self.line_number_area_width(), 0, 0, 0)
        cr = self.contentsRect()
        self._gutter.setGeometry(QRect(cr.left(), cr.top(),
                                       self.line_number_area_width(), cr.height()))

    def setFont(self, font):
        # The gutter width depends on the font metrics, so recompute it (and the
        # viewport margin) whenever the font changes. Without this, the margin is
        # stale from construction time and text gets clipped on the left until a
        # scroll/resize event forces a recompute.
        super().setFont(font)
        self._update_gutter_width()

    def showEvent(self, event):
        # On first show the widget finally has real geometry, so recompute the
        # gutter width/margin now. Without this the text is laid out against a
        # stale (construction-time) viewport margin and gets clipped on the left
        # until a scroll forces a re-layout. Deferred to the event loop so it
        # runs after the tab is fully laid out.
        super().showEvent(event)
        QTimer.singleShot(0, self._reflow_gutter)

    def _reflow_gutter(self):
        # setViewportMargins is a no-op when the value is unchanged, which leaves
        # the text painted under the gutter. Zero the margin first to force Qt to
        # re-lay-out the document against the correct margin.
        self.setViewportMargins(0, 0, 0, 0)
        self._update_gutter_width()
        self.viewport().update()

    def _update_gutter(self, rect, dy):
        if dy:
            self._gutter.scroll(0, dy)
        else:
            self._gutter.update(0, rect.y(), self._gutter.width(), rect.height())
        if rect.contains(self.viewport().rect()):
            self._update_gutter_width()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        cr = self.contentsRect()
        self._gutter.setGeometry(QRect(cr.left(), cr.top(),
                                       self.line_number_area_width(), cr.height()))

    def paint_line_numbers(self, event):
        p = QPainter(self._gutter)
        p.fillRect(event.rect(), self._gutter_bg)
        block = self.firstVisibleBlock()
        num = block.blockNumber()
        top = self.blockBoundingGeometry(block).translated(
            self.contentOffset()).top()
        bottom = top + self.blockBoundingRect(block).height()
        p.setPen(self._gutter_fg)
        f = self.font()
        p.setFont(f)
        w = self._gutter.width() - 5
        h = self.fontMetrics().height()
        while block.isValid() and top <= event.rect().bottom():
            if block.isVisible() and bottom >= event.rect().top():
                p.drawText(0, int(top), w, h, Qt.AlignRight, str(num + 1))
            block = block.next()
            top = bottom
            bottom = top + self.blockBoundingRect(block).height()
            num += 1
        p.end()


class TextEditor(QWidget):
    """Editor widget. dirty_changed/saved fire so the host can retitle the tab."""

    dirty_changed = Signal(bool)
    saved = Signal()

    def __init__(self, path: Path, display_name: str = "", parent=None):
        super().__init__(parent)
        self.setObjectName("TextEditor")
        self._path = path
        self._name = display_name or path.name
        self._original = ""
        self._dirty = False
        self._matches: list[int] = []     # match start positions
        self._match_idx = -1
        self._build()
        bind_theme(self, roles={
            "BG_HEADER", "TEXT_DIM", "ACCENT", "TEXT_ON_ACCENT",
        })
        self._do_load()

    def refresh_theme(self, p: dict) -> None:
        if not hasattr(self, "_edit"):
            return
        self._edit._gutter_bg = qc(p, "BG_HEADER")
        self._edit._gutter_fg = qc(p, "TEXT_DIM")
        self._edit._gutter.update()
        self._hl_format.setBackground(qc(p, "ACCENT"))
        self._hl_format.setForeground(qc(p, "TEXT_ON_ACCENT"))
        if self._find.text():
            self._on_find(self._find.text())

    # -- construction -------------------------------------------------------
    def _build(self):
        p = active_palette()
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)

        # Header: file name + Find box + Save/Revert.
        header = QWidget()
        header.setObjectName("HeaderBar")
        hb = QHBoxLayout(header)
        hb.setContentsMargins(8, 4, 8, 4)
        hb.setSpacing(6)
        self._label = QLabel(self._name)
        self._label.setStyleSheet(f"color:{_c(p,'TEXT_MAIN')}; font-weight:600;")
        hb.addWidget(self._label, 1)
        self._find = QLineEdit()
        self._find.setPlaceholderText(self.tr("Find…"))
        self._find.setClearButtonEnabled(True)
        self._find.setMaximumWidth(200)
        self._find.textChanged.connect(self._on_find)
        self._find.returnPressed.connect(self._find_next)
        hb.addWidget(self._find)
        self._prev_btn = QPushButton()
        bind_theme_icon(self._prev_btn, "arrow.png", 14, "TEXT_MAIN",
                        degrees=180)
        self._prev_btn.setFixedWidth(28)
        self._prev_btn.setToolTip(self.tr("Previous match"))
        self._prev_btn.clicked.connect(self._find_prev)
        hb.addWidget(self._prev_btn)
        self._next_btn = QPushButton()
        bind_theme_icon(self._next_btn, "arrow.png", 14, "TEXT_MAIN",
                        degrees=0)
        self._next_btn.setFixedWidth(28)
        self._next_btn.setToolTip(self.tr("Next match"))
        self._next_btn.clicked.connect(self._find_next)
        hb.addWidget(self._next_btn)
        self._match_label = QLabel("")
        self._match_label.setStyleSheet(f"color:{_c(p,'TEXT_DIM')};")
        self._match_label.setMinimumWidth(54)
        hb.addWidget(self._match_label)
        self._revert_btn = QPushButton(self.tr("Revert"))
        self._revert_btn.clicked.connect(self.revert)
        hb.addWidget(self._revert_btn)
        self._save_btn = QPushButton(self.tr("Save"))
        self._save_btn.setObjectName("PrimaryButton")
        self._save_btn.setToolTip(self.tr("Save (Ctrl+S)"))
        self._save_btn.clicked.connect(self.save)
        hb.addWidget(self._save_btn)
        v.addWidget(header)

        self._edit = _CodeEditor(_c(p, "BG_HEADER"), _c(p, "TEXT_DIM"))
        self._edit.setObjectName("TextEditorBody")
        self._edit.setLineWrapMode(QPlainTextEdit.NoWrap)
        f = QFont("monospace"); f.setStyleHint(QFont.Monospace); f.setPixelSize(13)
        self._edit.setFont(f)
        self._edit.setStyleSheet(
            f"QPlainTextEdit{{background:{_c(p,'BG_DEEP')};"
            f" color:{_c(p,'TEXT_MAIN')}; border:none; padding:4px 0px;}}")
        self._edit.textChanged.connect(self._on_text_changed)
        v.addWidget(self._edit, 1)

        self._hl_format = QTextCharFormat()
        self._hl_format.setBackground(qc(p, "ACCENT"))
        self._hl_format.setForeground(qc(p, "TEXT_ON_ACCENT"))

        self._save_sc = QShortcut(QKeySequence.Save, self)
        self._save_sc.setContext(Qt.WidgetWithChildrenShortcut)
        self._save_sc.activated.connect(self._save_shortcut)

    # -- load / save --------------------------------------------------------
    def load_file(self, path: Path, display_name: str = ""):
        """Swap the edited file in place (reuse-one-editor)."""
        self._path = path
        self._name = display_name or path.name
        self._label.setText(self._name)
        self._do_load()

    def _do_load(self):
        try:
            self._original = self._path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            self._original = ""
        self._edit.blockSignals(True)
        self._edit.setPlainText(self._original)
        self._edit.blockSignals(False)
        self._set_dirty(False)

    def _save_shortcut(self):
        # Don't rewrite an unchanged file - it would bump the mtime for nothing.
        if self._dirty:
            self.save()

    def save(self):
        try:
            content = self._edit.toPlainText()
            self._path.write_text(content, encoding="utf-8")
            self._original = content
            self._set_dirty(False)
            self.saved.emit()
        except OSError as exc:
            from gui_qt.confirm_overlay import ConfirmOverlay
            ConfirmOverlay.show_message(
                self, "Save failed", f"Could not save {self._name}:\n{exc}")

    def revert(self):
        self._edit.blockSignals(True)
        self._edit.setPlainText(self._original)
        self._edit.blockSignals(False)
        self._set_dirty(False)

    @property
    def name(self) -> str:
        return self._name

    @property
    def is_dirty(self) -> bool:
        return self._dirty

    # -- dirty tracking -----------------------------------------------------
    def _on_text_changed(self):
        self._set_dirty(self._edit.toPlainText() != self._original)

    def _set_dirty(self, dirty: bool):
        if dirty != self._dirty:
            self._dirty = dirty
            self.dirty_changed.emit(dirty)

    # -- find ---------------------------------------------------------------
    def find_text(self, keyword: str):
        """Pre-fill Find with *keyword* and jump to the first match (used when a
        file is opened from a content search)."""
        self._find.setText(keyword or "")

    def _on_find(self, text: str):
        # Clear old highlights, then highlight all matches of the query.
        self._edit.setExtraSelections([])
        self._matches = []
        self._match_idx = -1
        needle = (text or "").casefold()
        if not needle:
            self._update_match_label()
            return
        hay = self._edit.toPlainText().casefold()
        sels = []
        start = 0
        while True:
            i = hay.find(needle, start)
            if i < 0:
                break
            self._matches.append(i)
            sel = QTextEdit.ExtraSelection()
            sel.format = self._hl_format
            cur = self._edit.textCursor()
            cur.setPosition(i)
            cur.setPosition(i + len(needle), QTextCursor.KeepAnchor)
            sel.cursor = cur
            sels.append(sel)
            start = i + len(needle)
        self._edit.setExtraSelections(sels)
        if self._matches:
            self._goto_match(0)
        else:
            self._update_match_label()

    def _find_next(self):
        if not self._matches:
            return
        self._goto_match((self._match_idx + 1) % len(self._matches))

    def _find_prev(self):
        if not self._matches:
            return
        self._goto_match((self._match_idx - 1) % len(self._matches))

    def _goto_match(self, idx: int):
        self._match_idx = idx
        needle_len = len(self._find.text())
        pos = self._matches[idx]
        cur = self._edit.textCursor()
        cur.setPosition(pos)
        cur.setPosition(pos + needle_len, QTextCursor.KeepAnchor)
        self._edit.setTextCursor(cur)
        self._edit.ensureCursorVisible()
        self._update_match_label()

    def _update_match_label(self):
        n = len(self._matches)
        if n == 0:
            self._match_label.setText("" if not self._find.text() else "0/0")
        else:
            self._match_label.setText(f"{self._match_idx + 1}/{n}")
        self._prev_btn.setEnabled(n > 1)
        self._next_btn.setEnabled(n > 1)
