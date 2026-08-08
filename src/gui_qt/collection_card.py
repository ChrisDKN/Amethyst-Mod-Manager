"""A single Nexus Collection card (view-only) for the collections browser.

Collection tile images are PORTRAIT, so the card is portrait: a tall cover on
top, then name, author, a fixed-height clipped DESCRIPTION (the Tk card hides the
summary as a tooltip - here it's visible, clipped so every card is the same height
and the grid stays uniform), a stats bar, and a single View button (the install /
detail flow is a separate feature).

Thumbnails use ``nexus_mod_card.ThumbnailLoader`` with a PORTRAIT crop size, keyed
by the collection id.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import QWidget, QVBoxLayout, QLabel, QPushButton

from gui_qt.theme_qt import active_palette, _c
from gui_qt.nexus_mod_card import _TwoLineLabel, _fmt_count, cap_summary, wrap_tooltip

CARD_W = 240          # portrait card (Tk collection tile was ~220 wide)
IMG_W = 238           # cover width = CARD_W minus the 1px border each side
IMG_H = 272           # ~0.875 tile aspect (238/272 ≈ Tk 200/228)
DESC_H = 76           # holds the capped summary (≤4 lines @ 12px) with headroom
CARD_H = 472          # fixed total → all cards identical height


class CollectionCard(QWidget):
    """View-only collection card. *entry* is a NexusCollection; *on_view(entry)*
    opens its Nexus page; *on_context(entry, global_pos)* is the right-click menu."""

    def __init__(self, entry, on_view, on_context=None, on_remove=None,
                 parent=None):
        super().__init__(parent)
        self.setObjectName("GameCard")
        # QWidget (unlike QFrame) only paints its stylesheet border/background
        # when WA_StyledBackground is set.
        self.setAttribute(Qt.WA_StyledBackground, True)
        # A second (Remove) button needs extra height on this card only.
        self.setFixedSize(CARD_W, CARD_H + (34 if on_remove is not None else 0))
        self.entry = entry
        self._on_context = on_context
        p = active_palette()
        self._pal = p
        dim = _c(p, "TEXT_DIM")
        # Explicit card chrome (border + rounded corners) so the frame is always
        # drawn regardless of child painting; :hover lights the border accent.
        self.setStyleSheet(
            f"#GameCard {{ background:{_c(p,'BG_PANEL')};"
            f" border:1px solid {_c(p,'BORDER')}; border-radius:8px; }}"
            f"#GameCard:hover {{ border:1px solid {_c(p,'ACCENT')}; }}")

        v = QVBoxLayout(self)
        v.setContentsMargins(1, 1, 1, 1)   # keep children inside the 1px border
        v.setSpacing(0)

        # --- cover image (portrait, top) -----------------------------------
        self._img = QLabel()
        self._img.setAlignment(Qt.AlignCenter)
        self._img.setFixedSize(IMG_W, IMG_H)
        self._img.setStyleSheet(
            f"background:{_c(p,'BG_DEEP')};"
            f" border-top-left-radius:7px; border-top-right-radius:7px;"
            f" color:{dim};")
        self._img.setText("…")
        v.addWidget(self._img)

        # --- body ----------------------------------------------------------
        body = QWidget()
        bl = QVBoxLayout(body)
        bl.setContentsMargins(10, 8, 10, 8)
        bl.setSpacing(3)

        title = _TwoLineLabel(entry.name or f"Collection {entry.id}")
        title.setObjectName("GameCardName")
        title.setStyleSheet(
            f"color:{_c(p,'TEXT_MAIN')}; font-weight:600; font-size:13px;")
        title.setFixedHeight(36)
        bl.addWidget(title)

        if entry.user_name:
            author = QLabel(self.tr("by {0}").format(entry.user_name))
            author.setStyleSheet(f"color:{dim}; font-size:11px;")
            author.setMaximumWidth(CARD_W - 22)
            bl.addWidget(author)

        # Description (summary) - length-capped (Nexus-style) so it never spills
        # past the fixed-height box; full text word-wrapped in the tooltip.
        summary = (entry.summary or "").strip()
        desc = QLabel(cap_summary(summary, limit=110))
        desc.setWordWrap(True)
        desc.setAlignment(Qt.AlignLeft | Qt.AlignTop)
        desc.setFixedHeight(DESC_H)
        desc.setStyleSheet(f"color:{dim}; font-size:12px;")
        if summary:
            desc.setToolTip(wrap_tooltip(summary))
        bl.addWidget(desc)

        bl.addStretch(1)

        # Stats bar: ♥ endorsements · ↓ downloads · N mods.
        stats = QLabel(
            self.tr("♥ {0}    ↓ {1}    {2} mods").format(
                _fmt_count(entry.endorsements),
                _fmt_count(entry.total_downloads),
                entry.mod_count))
        stats.setStyleSheet(
            f"color:{_c(p,'TEXT_MAIN')}; font-size:11px;"
            f" border-top:1px solid {_c(p,'BORDER')}; padding-top:5px;")
        bl.addWidget(stats)

        # Single View button (view-only - install/detail is a later feature).
        view = QPushButton(self.tr("View"))
        view.setObjectName("GameAddBtn")          # blue accent
        view.setCursor(Qt.PointingHandCursor)
        view.clicked.connect(lambda: on_view(entry))
        bl.addWidget(view)

        if on_remove is not None:
            from gui_qt.theme_qt import _lighten
            danger = _c(p, "BTN_DANGER")
            remove = QPushButton(self.tr("Remove"))
            remove.setCursor(Qt.PointingHandCursor)
            remove.setStyleSheet(
                f"QPushButton {{ background:{danger};"
                f" color:#ffffff; border:none; border-radius:4px;"
                f" padding:4px 10px; }}"
                f"QPushButton:hover {{ background:{_lighten(danger)}; }}")
            remove.clicked.connect(lambda: on_remove(entry))
            bl.addWidget(remove)

        v.addWidget(body, 1)

    def set_thumbnail(self, pm: QPixmap) -> None:
        if pm is not None and not pm.isNull():
            self._img.setText("")
            self._img.setPixmap(pm)

    def contextMenuEvent(self, event):
        if self._on_context is not None:
            self._on_context(self.entry, event.globalPos())
        else:
            super().contextMenuEvent(event)
