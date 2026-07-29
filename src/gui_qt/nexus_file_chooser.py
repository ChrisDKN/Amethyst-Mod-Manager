"""In-window overlay shown when a Nexus mod has more than one MAIN file — the
user picks which one to install. NOT a separate window: on Steam Deck gaming mode
a top-level window (even a QDialog) can open behind the app, so this is a
borderless child widget that covers the host with a dimmed backdrop and a centered
card. Qt equivalent of the Tk `_FileChooserOverlay`.

Usage:
    NexusFileChooser.show_over(host, mod_name, files, on_pick=callback)
`on_pick(file_or_None)` is called when the user picks (Install / double-click) or
cancels (Cancel / backdrop click).
"""

from __future__ import annotations

import re

from PySide6.QtCore import Qt, QEvent, QT_TRANSLATE_NOOP
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QListWidget, QListWidgetItem,
    QPushButton, QFrame, QTextEdit,
)

from gui_qt.theme_qt import active_palette, _c


def _fmt_size_bytes(b: int) -> str:
    b = int(b or 0)
    if b <= 0:
        return ""
    for unit, div in (("GB", 1024 ** 3), ("MB", 1024 ** 2), ("KB", 1024)):
        if b >= div:
            return f"{b / div:.1f}{unit}"
    return f"{b}B"


# Categories offered in the install picker (MAIN first … MISCELLANEOUS last,
# matching the section order on the Nexus site's Files tab).
# OLD_VERSION / ARCHIVED / REMOVED are intentionally excluded — superseded or
# deleted archives, not standalone installs.
_INSTALL_CATEGORIES = {"MAIN": 0, "UPDATE": 1, "OPTIONAL": 2, "MISCELLANEOUS": 3}
# UI strings marked for extraction with QT_TRANSLATE_NOOP (module-level, so no
# tr() context is available here); translated at the use site via self.tr().
_CATEGORY_LABEL = {"MAIN": QT_TRANSLATE_NOOP("NexusFileChooser", "Main"),
                   "UPDATE": QT_TRANSLATE_NOOP("NexusFileChooser", "Update"),
                   "OPTIONAL": QT_TRANSLATE_NOOP("NexusFileChooser", "Optional"),
                   "MISCELLANEOUS": QT_TRANSLATE_NOOP("NexusFileChooser", "Misc")}
# Section headers shown as separator rows (grouped in category order).
_CATEGORY_HEADER = {"MAIN": QT_TRANSLATE_NOOP("NexusFileChooser", "Main files"),
                    "UPDATE": QT_TRANSLATE_NOOP("NexusFileChooser", "Update files"),
                    "OPTIONAL": QT_TRANSLATE_NOOP("NexusFileChooser", "Optional files"),
                    "MISCELLANEOUS": QT_TRANSLATE_NOOP("NexusFileChooser", "Miscellaneous files")}

# Marks a non-selectable section-header row (vs a real file row).
_HEADER_ROLE = Qt.UserRole + 1


def _plain_text(html_or_bbcode: str) -> str:
    """Reduce a Nexus file description (HTML and/or BBCode) to readable plain
    text — the pane shows text only, so strip markup rather than render it."""
    s = html_or_bbcode or ""
    s = s.replace("<br>", "\n").replace("<br/>", "\n").replace("<br />", "\n")
    s = re.sub(r"<[^>]+>", "", s)                 # HTML tags
    s = re.sub(r"\[/?[^\]]+\]", "", s)            # BBCode tags
    s = (s.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
          .replace("&quot;", '"').replace("&#39;", "'").replace("&nbsp;", " "))
    s = re.sub(r"\n{3,}", "\n\n", s)              # collapse blank runs
    return s.strip()


def installable_files(files: list) -> list:
    """Files to offer for install: MAIN + UPDATE + OPTIONAL + MISCELLANEOUS,
    sorted by category (main first) then newest-first. Falls back to every file
    when the mod exposes none of those categories (e.g. odd/empty category
    metadata)."""
    picks = [f for f in files
             if (f.category_name or "").upper() in _INSTALL_CATEGORIES]
    picks = picks or list(files)
    picks.sort(key=lambda f: (
        _INSTALL_CATEGORIES.get((f.category_name or "").upper(), 9),
        -(getattr(f, "uploaded_timestamp", 0) or 0)))
    return picks


class NexusFileChooser(QWidget):
    """A dimmed, click-absorbing backdrop with a centered card. Lives inside the
    host widget (no separate top-level window)."""

    CARD_W = 560
    CARD_H = 560

    def __init__(self, host: QWidget, mod_name: str, files: list, on_pick):
        super().__init__(host)
        self._host = host
        self._on_pick = on_pick
        self._done = False
        p = active_palette()

        # Full-host dimmed backdrop (absorbs clicks → cancel). Scope the
        # stylesheet to this widget's objectName so the semi-transparent black
        # does NOT cascade into the card's child labels (which would paint a
        # black band behind the text).
        self.setObjectName("OverlayBackdrop")
        self.setStyleSheet("#OverlayBackdrop { background: rgba(0,0,0,150); }")
        self.setGeometry(host.rect())

        # Centered card.
        self._card = QFrame(self)
        self._card.setObjectName("_FileChooserCard")
        self._card.setStyleSheet(
            f"#_FileChooserCard {{ background:{_c(p,'BG_PANEL')};"
            f" border:1px solid {_c(p,'BORDER')}; border-radius:8px; }}")
        v = QVBoxLayout(self._card)
        v.setContentsMargins(16, 14, 16, 14)
        v.setSpacing(8)

        hdr = QLabel(self.tr("'{0}' has multiple files.").format(mod_name))
        hdr.setStyleSheet(
            f"color:{_c(p,'TEXT_MAIN')}; font-weight:600; font-size:16px;")
        hdr.setWordWrap(True)
        v.addWidget(hdr)
        sub = QLabel(self.tr("Select which file to install:"))
        sub.setStyleSheet(f"color:{_c(p,'TEXT_DIM')}; font-size:13px;")
        v.addWidget(sub)

        self._list = QListWidget()
        self._list.setAlternatingRowColors(True)
        self._list.setStyleSheet(
            f"QListWidget {{ font-size:14px; background:{_c(p,'BG_LIST')};"
            f" border:1px solid {_c(p,'BORDER')}; border-radius:6px; }}"
            f"QListWidget::item {{ padding:8px 6px; color:{_c(p,'TEXT_MAIN')};"
            f" border-bottom:1px solid {_c(p,'BORDER')}; }}"
            f"QListWidget::item:selected {{ background:{_c(p,'BG_SELECT')};"
            f" color:{_c(p,'TEXT_ON_ACCENT')}; }}")
        last_cat = None
        for f in files:
            up = (f.category_name or "").upper()
            if up != last_cat:
                last_cat = up
                self._add_header(
                    self.tr(_CATEGORY_HEADER.get(up, _CATEGORY_LABEL.get(up, up))), p)
            name = f.name or f.file_name or f"File {f.file_id}"
            size = (f.size_in_bytes or 0) or (f.size_kb * 1024 if f.size_kb else 0)
            bits = []
            if f.version:
                bits.append(f"v{f.version}")
            sz = _fmt_size_bytes(size)
            if sz:
                bits.append(sz)
            detail = "   —   ".join(bits)
            item = QListWidgetItem(f"{name}\n{detail}" if detail else name)
            item.setData(Qt.UserRole, f)
            self._list.addItem(item)
        self._list.itemDoubleClicked.connect(lambda _i: self._pick())
        self._list.currentItemChanged.connect(self._on_row_changed)
        v.addWidget(self._list, 1)

        # Description of the selected file (plain text; may be empty).
        self._desc = QTextEdit()
        self._desc.setReadOnly(True)
        self._desc.setFixedHeight(110)
        self._desc.setStyleSheet(
            f"QTextEdit {{ font-size:13px; background:{_c(p,'BG_LIST')};"
            f" color:{_c(p,'TEXT_DIM')}; border:1px solid {_c(p,'BORDER')};"
            f" border-radius:6px; padding:6px; }}")
        v.addWidget(self._desc)

        self._select_first_file()

        bar = QHBoxLayout()
        bar.addStretch(1)
        cancel = QPushButton(self.tr("Cancel"))
        cancel.setObjectName("FormButton")
        cancel.setCursor(Qt.PointingHandCursor)
        cancel.clicked.connect(lambda: self._finish(None))
        bar.addWidget(cancel)
        install = QPushButton(self.tr("Install"))
        install.setObjectName("PrimaryButton")    # blue accent, matches other overlays
        install.setCursor(Qt.PointingHandCursor)
        install.clicked.connect(self._pick)
        bar.addWidget(install)
        v.addLayout(bar)

        host.installEventFilter(self)
        self._reposition()
        self.show()
        self.raise_()

    @classmethod
    def show_over(cls, host, mod_name, files, on_pick):
        # Anchor to the top-level window so the backdrop covers the whole app.
        top = host.window() if host is not None else None
        return cls(top or host, mod_name, files, on_pick)

    # -- internals ----------------------------------------------------------
    def _add_header(self, text: str, p):
        """Append a non-selectable section-header row.

        The row text is drawn by a QLabel via setItemWidget rather than the
        item's own text — the QListWidget::item stylesheet hard-sets `color`,
        which overrides QListWidgetItem.setForeground, so an item-level colour
        would be ignored. A child QLabel is styled independently."""
        item = QListWidgetItem("")
        item.setData(_HEADER_ROLE, True)
        item.setFlags(Qt.NoItemFlags)              # not selectable / not focusable
        self._list.addItem(item)
        lbl = QLabel(text)
        lbl.setStyleSheet(
            f"color:{_c(p,'TONE_GREEN')}; font-weight:700; font-size:13px;"
            f" padding:2px 4px; background:transparent;")
        self._list.setItemWidget(item, lbl)

    def _is_header(self, item) -> bool:
        return item is not None and bool(item.data(_HEADER_ROLE))

    def _select_first_file(self):
        for i in range(self._list.count()):
            if not self._is_header(self._list.item(i)):
                self._list.setCurrentRow(i)
                return

    def _on_row_changed(self, cur, _prev):
        """Update the description pane for the selected file row."""
        f = cur.data(Qt.UserRole) if (cur is not None and not self._is_header(cur)) else None
        text = _plain_text(getattr(f, "description", "")) if f is not None else ""
        self._desc.setPlainText(text or self.tr("No description provided."))

    def _reposition(self):
        self.setGeometry(self._host.rect())
        w = min(self.CARD_W, self._host.width() - 40)
        h = min(self.CARD_H, self._host.height() - 40)
        self._card.setFixedSize(max(320, w), max(240, h))
        self._card.move((self.width() - self._card.width()) // 2,
                        (self.height() - self._card.height()) // 2)

    def _pick(self):
        item = self._list.currentItem()
        if self._is_header(item):
            return                                 # ignore section-header rows
        self._finish(item.data(Qt.UserRole) if item is not None else None)

    def _finish(self, result):
        if self._done:
            return
        self._done = True
        self._host.removeEventFilter(self)
        cb = self._on_pick
        self.hide()
        self.deleteLater()
        if cb is not None:
            cb(result)

    def mousePressEvent(self, event):
        # Click on the dim backdrop (outside the card) cancels.
        if not self._card.geometry().contains(event.position().toPoint()):
            self._finish(None)

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Escape:
            self._finish(None)
        else:
            super().keyPressEvent(event)

    def eventFilter(self, obj, event):
        if obj is self._host and event.type() == QEvent.Resize:
            self._reposition()
        return super().eventFilter(obj, event)
