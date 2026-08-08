"""Mod-note editor - borderless in-window overlay.

Edit the free-text note attached to a mod (or apply one to several). A dimmed
child overlay (see gui_qt/overlay_base.py) with a multiline text box, Save /
Cancel, and an optional Remove. Qt port of the Tk note editor
(gui/modlist_panel._open_note_editor_by_name / _for_multi).

``on_save(text)`` is called on Save; ``on_remove()`` on Remove. All widgets built
once with real parents.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QWidget, QHBoxLayout, QLabel, QPushButton, QTextEdit,
)

from gui_qt.overlay_base import OverlayBase
from gui_qt.theme_qt import active_palette, _c


class NoteEditorOverlay(OverlayBase):
    CARD_W = 520
    CARD_H = 320
    MIN_W = 360
    MIN_H = 220

    def __init__(self, host: QWidget, title: str, initial: str,
                 on_save, on_remove, allow_remove: bool = False):
        super().__init__(host)
        self._on_save = on_save
        self._on_remove = on_remove
        p = active_palette()

        _card, v = self._make_card("_NoteCard")

        title_lbl = QLabel(self.tr("Note - {0}").format(title))
        title_lbl.setStyleSheet(
            f"color:{_c(p,'TEXT_MAIN')}; font-weight:600; font-size:15px;")
        v.addWidget(title_lbl)

        self._edit = QTextEdit()
        self._edit.setPlainText(initial or "")
        self._edit.setStyleSheet(
            f"QTextEdit {{ background:{_c(p,'BG_LIST')}; color:{_c(p,'TEXT_MAIN')};"
            f" border:1px solid {_c(p,'BORDER')}; border-radius:4px; }}")
        v.addWidget(self._edit, 1)

        hint = QLabel(self.tr("Markdown is supported - it renders in the note tooltip."))
        hint.setStyleSheet(
            f"color:{_c(p,'TEXT_DIM')}; font-size:11px;")
        v.addWidget(hint)

        bar = QHBoxLayout()
        if allow_remove:
            rm = QPushButton(self.tr("Remove note"))
            rm.setObjectName("FormButton")
            rm.setCursor(Qt.PointingHandCursor)
            rm.clicked.connect(self._remove)
            bar.addWidget(rm)
        bar.addStretch(1)
        cancel = QPushButton(self.tr("Cancel"))
        cancel.setObjectName("FormButton")
        cancel.setCursor(Qt.PointingHandCursor)
        cancel.clicked.connect(lambda: self._finish())
        bar.addWidget(cancel)
        save = QPushButton(self.tr("Save"))
        save.setObjectName("PrimaryButton")
        save.setCursor(Qt.PointingHandCursor)
        save.clicked.connect(self._save)
        bar.addWidget(save)
        v.addLayout(bar)

        self._present()
        self._edit.setFocus()

    @classmethod
    def show_over(cls, host, title, initial, on_save, on_remove,
                  allow_remove=False):
        top = host.window() if host is not None else None
        return cls(top or host, title, initial, on_save, on_remove,
                   allow_remove=allow_remove)

    # -- internals ----------------------------------------------------------
    def _save(self):
        if self._done:
            return
        text = self._edit.toPlainText()
        self._finish()
        if self._on_save is not None:
            self._on_save(text)

    def _remove(self):
        if self._done:
            return
        self._finish()
        if self._on_remove is not None:
            self._on_remove()
