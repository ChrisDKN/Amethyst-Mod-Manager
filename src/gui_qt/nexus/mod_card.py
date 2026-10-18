"""A Nexus mod card using the shared card widgets.

`NexusModCard` mirrors the nexusmods.com browse card: cover image, title, author,
category, "updated X ago / uploaded date", a multi-line description, and a bottom
stats bar (endorsements / downloads / size), with View / Install buttons. It uses
the `#GameCard` chrome so it matches the rest of the Qt UI. Thumbnails are fetched
off the UI thread by the shared `ThumbnailLoader` and delivered back via a Qt signal (keyed by
mod_id), with a small in-memory LRU cache shared across the browser (mirrors the Tk
`IMG_CACHE_MAX`).

All displayed fields come from the single GraphQL list call (NexusModInfo) - no
extra per-card API request, so the card is rate-limit-free.
"""

from __future__ import annotations

from datetime import datetime, timezone

from PySide6.QtCore import Qt, QEvent, QCoreApplication
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QFrame,
    QCheckBox,
)

from gui_qt.appearance.theme import active_palette, _c, contrast_text
from Utils.ui import config as uc
from gui_qt.widgets.card_helpers import (
    CARD_W, CARD_H, IMG_W, IMG_H, _TwoLineLabel, _fmt_count, cap_summary, wrap_tooltip,
)


def _fmt_size_kb(kb: int) -> str:
    """File size in KB → human string (KB/MB/GB)."""
    kb = int(kb or 0)
    if kb <= 0:
        return ""
    if kb >= 1024 * 1024:
        return f"{kb / (1024 * 1024):.1f}GB"
    if kb >= 1024:
        return f"{kb / 1024:.1f}MB"
    return f"{kb}KB"


def _parse_iso(s: str):
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None


def _ago(s: str) -> str:
    """ISO timestamp → '3 days ago' / '4 months ago' (matches the Nexus card)."""
    dt = _parse_iso(s)
    if dt is None:
        return ""
    now = datetime.now(timezone.utc)
    secs = max(0, (now - dt).total_seconds())
    mins = secs / 60
    hours = mins / 60
    days = hours / 24
    # Module-level fn (no QObject `self`), so translate via QCoreApplication.
    _t = lambda s: QCoreApplication.translate("nexus_mod_card", s)
    if days >= 365:
        v = int(days / 365)
        return (_t("{0} year ago") if v == 1 else _t("{0} years ago")).format(v)
    if days >= 30:
        v = int(days / 30)
        return (_t("{0} month ago") if v == 1 else _t("{0} months ago")).format(v)
    if days >= 1:
        v = int(days)
        return (_t("{0} day ago") if v == 1 else _t("{0} days ago")).format(v)
    if hours >= 1:
        v = int(hours)
        return (_t("{0} hour ago") if v == 1 else _t("{0} hours ago")).format(v)
    if mins >= 1:
        v = int(mins)
        return (_t("{0} minute ago") if v == 1 else _t("{0} minutes ago")).format(v)
    return _t("just now")


def _fmt_date(s: str) -> str:
    """Format a Nexus upload timestamp for display."""
    dt = _parse_iso(s)
    if dt is None:
        return ""
    return dt.strftime(uc.display_date_pattern())


class NexusModCard(QFrame):
    """One mod in the grid. *entry* is a NexusModInfo. Callbacks:
        on_view(entry)     - show the mod's details
        on_install(entry)  - download + install
        on_context(entry, global_pos) - show a right-click menu (optional)
    """

    def __init__(self, entry, on_view, on_install, on_context=None,
                 is_installed: bool = False, download_only: bool = False,
                 on_select=None, selected: bool = False, parent=None):
        super().__init__(parent)
        self.setObjectName("GameCard")
        self.setFixedSize(CARD_W, CARD_H)
        self.entry = entry
        self._on_context = on_context
        self._installed = bool(is_installed)
        self._watching = False
        self._download_only = bool(download_only)
        p = active_palette()
        self._pal = p

        v = QVBoxLayout(self)
        # QFrame's styled 1px border already insets the layout's content rect.
        # IMG_W matches that inner width so the cover cannot paint over it.
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)

        # --- cover image (full-bleed top) ----------------------------------
        self._img = QLabel()
        self._img.setObjectName("NexusCardImage")
        self._img.setAlignment(Qt.AlignCenter)
        self._img.setFixedSize(IMG_W, IMG_H)
        self._img.setText("…")
        v.addWidget(self._img)
        self._select_cb = None
        self._select_press_pos = None
        if on_select is not None:
            cb = QCheckBox(self._img)
            cb.setObjectName("CardSelect")
            cb.setToolTip(self.tr("Select for Download selected"))
            cb.setAccessibleName(self.tr("Select {0} for Download selected").format(
                entry.name or f"Mod {entry.mod_id}"))
            cb.setCursor(Qt.PointingHandCursor)
            cb.setChecked(selected)
            cb.toggled.connect(lambda on: on_select(entry, on))
            cb.move(8, 8)
            self._select_cb = cb

        # --- body ----------------------------------------------------------
        body = QWidget()
        bl = QVBoxLayout(body)
        bl.setContentsMargins(10, 8, 10, 8)
        bl.setSpacing(3)

        title = _TwoLineLabel(entry.name or f"Mod {entry.mod_id}")
        title.setObjectName("NexusCardTitle")
        title.setFixedHeight(36)
        bl.addWidget(title)

        if entry.author:
            author = QLabel(self.tr("by {0}").format(entry.author))
            author.setObjectName("NexusCardAuthor")
            author.setMaximumWidth(CARD_W - 20)
            bl.addWidget(author)

        if entry.category_name:
            cat = QLabel(entry.category_name)
            cat.setObjectName("NexusCardCategory")
            bl.addWidget(cat)

        # "updated X ago · uploaded date" (only the parts we have).
        ago = _ago(entry.updated_at)
        up = _fmt_date(entry.created_at)
        date_bits = []
        if ago:
            date_bits.append(f"⟳ {ago}")
        if up:
            date_bits.append(f"⬆ {up}")
        self._dates_label = None
        if date_bits:
            dates = QLabel("   ".join(date_bits))
            dates.setObjectName("NexusCardDates")
            bl.addWidget(dates)
            self._dates_label = dates

        # Description (the summary) - fills the remaining space, length-capped
        # (Nexus-style) with the full text word-wrapped in the tooltip.
        _summary = (entry.summary or "").strip()
        desc = QLabel(cap_summary(_summary))
        desc.setObjectName("NexusCardSummary")
        desc.setWordWrap(True)
        desc.setAlignment(Qt.AlignLeft | Qt.AlignTop)
        if _summary:
            desc.setToolTip(wrap_tooltip(_summary))
        desc.setSizePolicy(desc.sizePolicy().horizontalPolicy(),
                           desc.sizePolicy().Policy.Ignored)
        bl.addWidget(desc, 1)

        # Stats bar: ♥ endorsements · ↓ downloads · ▢ size.
        sbits = [f"♥ {_fmt_count(entry.endorsement_count)}",
                 f"↓ {_fmt_count(entry.downloads_total)}"]
        size = _fmt_size_kb(getattr(entry, "file_size_kb", 0))
        if size:
            sbits.append(f"▢ {size}")
        stats = QLabel("    ".join(sbits))
        stats.setObjectName("NexusCardStats")
        bl.addWidget(stats)

        # Buttons: View (blue) + Install (green).
        row = QHBoxLayout()
        row.setSpacing(6)
        view = QPushButton(self.tr("View"))
        view.setObjectName("GameAddBtn")          # blue accent
        view.setCursor(Qt.PointingHandCursor)
        view.clicked.connect(lambda: on_view(entry))
        row.addWidget(view, 1)
        self._install_btn = QPushButton()
        self._install_btn.setObjectName("GameSelectBtn")    # green base style
        self._install_btn.setCursor(Qt.PointingHandCursor)
        self._install_btn.clicked.connect(lambda: on_install(entry))
        row.addWidget(self._install_btn, 1)
        bl.addLayout(row)
        self._apply_install_style()

        v.addWidget(body, 1)
        if on_select is not None:
            self.setCursor(Qt.PointingHandCursor)
            for child in self.findChildren(QWidget):
                if not isinstance(child, (QPushButton, QCheckBox)):
                    child.installEventFilter(self)

    def refresh_date_format(self):
        if self._dates_label is None:
            return
        bits = []
        ago = _ago(self.entry.updated_at)
        uploaded = _fmt_date(self.entry.created_at)
        if ago:
            bits.append(f"⟳ {ago}")
        if uploaded:
            bits.append(f"⬆ {uploaded}")
        self._dates_label.setText("   ".join(bits))

    def set_selected(self, selected: bool) -> None:
        if self._select_cb is None:
            return
        blocked = self._select_cb.blockSignals(True)
        self._select_cb.setChecked(selected)
        self._select_cb.blockSignals(blocked)

    def _toggle_selected(self):
        if self._select_cb is not None:
            self._select_cb.toggle()

    def _finish_select_click(self, event):
        start = self._select_press_pos
        self._select_press_pos = None
        if (start is not None and
                (event.globalPosition().toPoint() - start).manhattanLength()
                <= QApplication.startDragDistance()):
            self._toggle_selected()

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton and self._select_cb is not None:
            self._select_press_pos = event.globalPosition().toPoint()
            event.accept()
        else:
            super().mousePressEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton and self._select_cb is not None:
            self._finish_select_click(event)
            event.accept()
        else:
            super().mouseReleaseEvent(event)

    def eventFilter(self, obj, event):
        if (self._select_cb is not None
                and event.type() in (QEvent.MouseButtonPress, QEvent.MouseButtonRelease)
                and event.button() == Qt.LeftButton):
            if event.type() == QEvent.MouseButtonPress:
                self._select_press_pos = event.globalPosition().toPoint()
            elif event.type() == QEvent.MouseButtonRelease:
                self._finish_select_click(event)
                return True
        return super().eventFilter(obj, event)

    def set_installed(self, installed: bool) -> None:
        if bool(installed) == self._installed:
            return
        self._installed = bool(installed)
        self._apply_install_style()

    def set_download_only(self, on: bool) -> None:
        """Install/Reinstall become Download/Redownload."""
        if bool(on) == self._download_only:
            return
        self._download_only = bool(on)
        self._apply_install_style()

    def set_watching(self, watching: bool) -> None:
        """A non-premium install is waiting for this mod's browser download -
        the Install button becomes a red Cancel (the click toggles the watch)."""
        if bool(watching) == self._watching:
            return
        self._watching = bool(watching)
        self._apply_install_style()

    def _apply_install_style(self):
        if self._watching:
            danger = _c(self._pal, "BTN_DANGER")
            self._install_btn.setText(self.tr("Cancel"))
            self._install_btn.setStyleSheet(
                f"QPushButton{{background:{danger}; color:{contrast_text(danger)}; font-weight:600;"
                f" border:none; border-radius:4px; padding:5px 0;}}"
                f"QPushButton:hover{{background:{danger};}}")
        elif self._installed:
            # Reinstall - orange (BTN_WARN), like the Downloads tab.
            warn = _c(self._pal, "BTN_WARN")
            self._install_btn.setText(self.tr("Redownload") if self._download_only
                                      else self.tr("Reinstall"))
            self._install_btn.setStyleSheet(
                f"QPushButton{{background:{warn}; color:{contrast_text(warn)}; font-weight:600;"
                f" border:none; border-radius:4px; padding:5px 0;}}"
                f"QPushButton:hover{{background:{warn};}}")
        else:
            # Install - clear the inline style so the green #GameSelectBtn QSS shows.
            self._install_btn.setText(self.tr("Download") if self._download_only
                                      else self.tr("Install"))
            if self._install_btn.styleSheet():
                self._install_btn.setStyleSheet("")

    def set_thumbnail(self, pm: QPixmap) -> None:
        if pm is not None and not pm.isNull():
            self._img.setText("")
            self._img.setPixmap(pm)

    def contextMenuEvent(self, event):
        if self._on_context is not None:
            self._on_context(self.entry, event.globalPos())
        else:
            super().contextMenuEvent(event)
