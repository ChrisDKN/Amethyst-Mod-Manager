"""In-window overlay shown when a mod's structure doesn't match the game and
auto-strip failed - the user types a prefix to install the files under (e.g.
``bin/x64`` for CET, ``archive/pc/mod`` for REDmod). Qt equivalent of the Tk
``_SetPrefixDialog``. A dimmed child overlay via gui_qt/overlay_base.py (NOT a
top-level window - Steam-Deck gaming mode opens those behind the app).

`on_done(result)` is called with:
    str   - install under this prefix ("" = install as-is, no remap)
    None  - cancel the install
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QWidget, QHBoxLayout, QLabel, QLineEdit, QPushButton, QPlainTextEdit,
)

from gui_qt.overlay_base import OverlayBase
from gui_qt.theme_qt import active_palette, _c


class SetPrefixOverlay(OverlayBase):
    CARD_W = 580
    CARD_H = 560
    MIN_W = 360
    MIN_H = 300

    def __init__(self, host: QWidget, mod_name: str, required: set,
                 file_list: list, on_done):
        super().__init__(host, on_done=on_done)
        self._file_list = file_list
        p = active_palette()

        _card, v = self._make_card("PrefixCard", margins=(16, 14, 16, 14),
                                   spacing=6, bg_key="BG_DEEP")

        if mod_name:
            mn = QLabel(self.tr("Mod: {0}").format(mod_name))
            mn.setStyleSheet(
                f"color:{_c(p,'ACCENT')}; font-weight:600; font-size:14px;")
            v.addWidget(mn)
        title = QLabel(self.tr("This mod has no recognised top-level folders."))
        title.setStyleSheet(
            f"color:{_c(p,'TEXT_MAIN')}; font-weight:600; font-size:14px;")
        title.setWordWrap(True)
        v.addWidget(title)
        if required:
            exp = QLabel(self.tr("Expected one of:  {0}").format(
                ",  ".join(sorted(required))))
            exp.setStyleSheet(f"color:{_c(p,'TEXT_DIM')}; font-size:12px;")
            exp.setWordWrap(True)
            v.addWidget(exp)

        prompt = QLabel(self.tr("Install all files under this path (e.g. archive/pc/mod):"))
        prompt.setStyleSheet(f"color:{_c(p,'TEXT_MAIN')}; font-size:13px;")
        v.addWidget(prompt)
        self._entry = QLineEdit()
        self._entry.setPlaceholderText(self.tr("e.g. bin/x64"))
        self._entry.textChanged.connect(self._refresh_preview)
        self._entry.returnPressed.connect(self._on_prefix)
        v.addWidget(self._entry)

        self._tree = QPlainTextEdit()
        self._tree.setReadOnly(True)
        self._tree.setLineWrapMode(QPlainTextEdit.NoWrap)
        self._tree.setStyleSheet(
            f"QPlainTextEdit{{background:{_c(p,'BG_PANEL')};"
            f" color:{_c(p,'TEXT_MAIN')}; border:1px solid {_c(p,'BORDER')};"
            f" border-radius:6px; font-family:monospace; font-size:12px;}}")
        v.addWidget(self._tree, 1)

        bar = QHBoxLayout()
        bar.addStretch(1)
        cancel = QPushButton(self.tr("Cancel"))
        cancel.setCursor(Qt.PointingHandCursor)
        cancel.clicked.connect(lambda: self._finish(None))
        bar.addWidget(cancel)
        as_is = QPushButton(self.tr("Install Anyway"))
        as_is.setCursor(Qt.PointingHandCursor)
        as_is.clicked.connect(lambda: self._finish(""))   # "" = install as-is
        bar.addWidget(as_is)
        use = QPushButton(self.tr("Install with Prefix"))
        use.setObjectName("GameAddBtn")     # blue accent
        use.setCursor(Qt.PointingHandCursor)
        use.clicked.connect(self._on_prefix)
        bar.addWidget(use)
        v.addLayout(bar)

        self._refresh_preview("")
        self._present()
        self._entry.setFocus()

    @classmethod
    def show_over(cls, host, mod_name, required, file_list, on_done):
        top = host.window() if host is not None else None
        return cls(top or host, mod_name, required, file_list, on_done)

    # -- internals ----------------------------------------------------------
    def _refresh_preview(self, _text=None):
        from Utils.text.tree import build_tree_str
        prefix = self._entry.text().strip().strip("/").replace("\\", "/")
        paths = []
        for _s, dst, is_folder in self._file_list:
            if is_folder:
                continue
            d = dst.replace("\\", "/")
            paths.append(f"{prefix}/{d}" if prefix else d)
        self._tree.setPlainText(build_tree_str(paths))

    def _on_prefix(self):
        self._finish(self._entry.text().strip())
