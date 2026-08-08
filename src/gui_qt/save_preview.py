"""Save preview - the details pane along the bottom of the Saves sub-tab.

Shows what MO2 shows for a Bethesda save: screenshot, character, play time and
the plugin list it was written with. Plugins missing from the active load
order are flagged red, so an unsafe save is obvious before loading it. Parsing
lives in Utils.save_header and runs on a worker; this only renders.
"""

from __future__ import annotations

import time

from PySide6.QtCore import Qt
from PySide6.QtGui import QBrush, QColor, QImage, QPixmap
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QListWidget, QListWidgetItem,
    QGridLayout, QSizePolicy, QFrame,
)

from gui_qt.theme_qt import active_palette, _c
from Utils.save_header import format_play_time

# Save screenshots are small (320x192 Skyrim, 512x288 New Vegas) - little to
# gain from blowing them up much past native size.
_SHOT_MIN_W = 200
_SHOT_MAX_W = 420

_DATE_FMT = "%d %b %Y  %H:%M"

# Format id → the label shown next to the save's engine version.
_KIND_LABELS = {
    "TESV": "Skyrim",
    "FO4": "Fallout 4",
    "FO3": "Fallout 3 / New Vegas",
    "TES4": "Oblivion",
}


class SavePreviewPane(QWidget):
    """Bottom pane of the Saves tab: screenshot + metadata + plugin list."""

    def __init__(self):
        super().__init__()
        self.setObjectName("SavePreviewPane")
        # Active load order, lowercased. Empty = unknown, which switches the
        # missing flagging off (else every plugin would look missing).
        self._known: set[str] = set()
        self._pixmap: "QPixmap | None" = None
        # The save the shown header came from - keys the lightbox tab.
        self._path = None
        self._build()
        self.clear()

    # ---- layout -----------------------------------------------------------
    def _build(self):
        p = active_palette()
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        top = QFrame()
        top.setFrameShape(QFrame.HLine)
        top.setStyleSheet(f"color:{_c(p,'BORDER')};")
        outer.addWidget(top)

        body = QWidget()
        body.setStyleSheet(f"background:{_c(p,'BG_DEEP')};")
        row = QHBoxLayout(body)
        row.setContentsMargins(10, 8, 10, 8)
        row.setSpacing(12)
        outer.addWidget(body, 1)

        # 1. Screenshot - click opens it full size, like the FOMOD wizard's.
        self._shot = QLabel()
        self._shot.setAlignment(Qt.AlignCenter)
        self._shot.setMinimumWidth(_SHOT_MIN_W)
        self._shot.setMaximumWidth(_SHOT_MAX_W)
        self._shot.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Expanding)
        self._shot.setStyleSheet(f"color:{_c(p,'TEXT_DIM')};")
        self._shot.mousePressEvent = lambda _e: self._open_lightbox()
        row.addWidget(self._shot, 0)

        # 2. Metadata grid, rebuilt per save - which fields a format carries
        #    varies (Oblivion has no race, New Vegas no timestamp).
        fields = QWidget()
        self._grid = QGridLayout(fields)
        self._grid.setContentsMargins(0, 0, 0, 0)
        self._grid.setHorizontalSpacing(10)
        self._grid.setVerticalSpacing(3)
        self._grid.setColumnStretch(1, 1)
        wrap = QVBoxLayout()
        wrap.setContentsMargins(0, 0, 0, 0)
        wrap.addWidget(fields)
        wrap.addStretch(1)
        holder = QWidget()
        holder.setLayout(wrap)
        row.addWidget(holder, 1)

        # 3. Plugin list.
        plugins = QVBoxLayout()
        plugins.setContentsMargins(0, 0, 0, 0)
        plugins.setSpacing(3)
        self._plugin_header = QLabel()
        self._plugin_header.setStyleSheet(
            f"color:{_c(p,'TEXT_DIM')}; font-weight:600;")
        plugins.addWidget(self._plugin_header)
        self._plugins = QListWidget()
        self._plugins.setSelectionMode(QListWidget.NoSelection)
        self._plugins.setUniformItemSizes(True)
        self._plugins.setStyleSheet(
            f"QListWidget {{ background:{_c(p,'BG_PANEL')}; border:none;"
            f" color:{_c(p,'TEXT_MAIN')}; }}")
        plugins.addWidget(self._plugins, 1)
        holder2 = QWidget()
        holder2.setLayout(plugins)
        holder2.setMinimumWidth(220)
        row.addWidget(holder2, 1)

        # Shown instead of everything above while reading / on failure.
        self._message = QLabel()
        self._message.setAlignment(Qt.AlignCenter)
        self._message.setStyleSheet(f"color:{_c(p,'TEXT_DIM')};")
        outer.addWidget(self._message, 1)
        self._body = body

    # ---- state ------------------------------------------------------------
    def set_known_plugins(self, names):
        """Tell the pane the active profile's load order, for missing flagging."""
        self._known = {str(n).lower() for n in (names or [])}

    def clear(self):
        self._show_message("")

    def set_message(self, text: str):
        """Show *text* instead of a save's details (reading / unreadable)."""
        self._show_message(text)

    def _show_message(self, text: str):
        self._pixmap = None
        self._message.setText(text)
        self._message.setVisible(bool(text))
        self._body.setVisible(not text)

    # ---- rendering --------------------------------------------------------
    def set_header(self, header, *, path=None, mtime: float = 0.0):
        """Render a parsed SaveHeader. *path* names and keys the lightbox tab;
        *mtime* backs the date for formats carrying no timestamp (FO3/NV)."""
        self._path = path
        if header is None:
            self._show_message(self.tr("This file could not be read as a save."))
            return
        self._show_message("")
        self._set_screenshot(header.screenshot)
        self._set_fields(header, mtime)
        self._set_plugins(header)

    def _set_screenshot(self, shot):
        if shot is None or not shot.data:
            self._pixmap = None
            self._shot.setPixmap(QPixmap())
            self._shot.setText(self.tr("No screenshot"))
            self._shot.setCursor(Qt.ArrowCursor)
            self._shot.setToolTip("")
            return
        fmt = QImage.Format_RGBA8888 if shot.channels == 4 else QImage.Format_RGB888
        # Pass the stride explicitly - QImage otherwise assumes 32-bit aligned
        # rows and skews any RGB888 image whose width isn't a multiple of 4.
        image = QImage(shot.data, shot.width, shot.height,
                       shot.width * shot.channels, fmt).copy()
        self._pixmap = QPixmap.fromImage(image)
        self._shot.setText("")
        self._shot.setCursor(Qt.PointingHandCursor)
        self._shot.setToolTip(self.tr("Click to open full size"))
        self._rescale()

    def _rescale(self):
        if self._pixmap is None:
            return
        area = self._shot.size()
        if area.width() <= 0 or area.height() <= 0:
            return
        self._shot.setPixmap(self._pixmap.scaled(
            area, Qt.KeepAspectRatio, Qt.SmoothTransformation))

    def resizeEvent(self, event):  # noqa: N802 (Qt override)
        super().resizeEvent(event)
        self._rescale()

    def _open_lightbox(self):
        """Open the screenshot full size in a tab. The pixels come out of the
        save, so the viewer takes a pixmap rather than a path."""
        if self._pixmap is None:
            return
        tabs = getattr(self.window(), "_tabs", None)
        if tabs is None:
            return
        from gui_qt.image_view import ImageView
        tabs.open_tab(ImageView(pixmap=self._pixmap), self.tr("Screenshot"),
                      key=f"save-shot:{self._path}" if self._path else None)

    def _set_fields(self, header, mtime: float):
        while self._grid.count():
            item = self._grid.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

        saved_at = header.saved_at or mtime
        rows = [
            (self.tr("Character"), header.character),
            (self.tr("Level"), str(header.level) if header.level else ""),
            (self.tr("Location"), header.location),
            (self.tr("Play time"), format_play_time(header.play_time)),
            (self.tr("Title"), header.title),
            (self.tr("Race"), _pretty_race(header.race)),
            (self.tr("Sex"), _sex_text(self, header.sex)),
            (self.tr("Saved"), time.strftime(_DATE_FMT, time.localtime(saved_at))
             if saved_at else ""),
            (self.tr("Save number"),
             str(header.save_number) if header.save_number else ""),
            (self.tr("Game"), _game_label(header)),
        ]
        p = active_palette()
        line = 0
        for label, value in rows:
            if not value:
                continue        # a format that doesn't carry this field
            key = QLabel(label)
            key.setStyleSheet(f"color:{_c(p,'TEXT_DIM')};")
            key.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            val = QLabel(value)
            val.setStyleSheet(f"color:{_c(p,'TEXT_MAIN')};")
            val.setTextInteractionFlags(Qt.TextSelectableByMouse)
            val.setWordWrap(True)
            self._grid.addWidget(key, line, 0)
            self._grid.addWidget(val, line, 1)
            line += 1

        if header.partial:
            warn = QLabel(self.tr("Only part of this save could be read."))
            warn.setStyleSheet(f"color:{_c(p,'TEXT_WARN')};")
            warn.setWordWrap(True)
            self._grid.addWidget(warn, line, 0, 1, 2)

    def _set_plugins(self, header):
        self._plugins.clear()
        regular, light = header.plugins, header.light_plugins
        if not regular and not light:
            self._plugin_header.setText(self.tr("Plugins"))
            self._plugins.addItem(
                self.tr("Not recorded in this save.") if not header.partial
                else self.tr("Could not be read."))
            return

        if light:
            self._plugin_header.setText(
                self.tr("Plugins ({0} · {1} ESL)").format(len(regular), len(light)))
        else:
            self._plugin_header.setText(
                self.tr("Plugins ({0})").format(len(regular)))

        p = active_palette()
        missing_brush = QBrush(QColor(_c(p, "TEXT_ERR")))
        dim_brush = QBrush(QColor(_c(p, "TEXT_DIM")))
        tip = self.tr("Not in this profile's load order.")
        # Regular first, then light ones dimmed - the game's load order.
        for name, is_light in ([(n, False) for n in regular]
                               + [(n, True) for n in light]):
            item = QListWidgetItem(name)
            if self._known and name.lower() not in self._known:
                item.setForeground(missing_brush)
                item.setToolTip(tip)
            elif is_light:
                item.setForeground(dim_brush)
            self._plugins.addItem(item)


def _game_label(header) -> str:
    """"Skyrim (v12)" - the format the save claims, not the handler's name."""
    label = _KIND_LABELS.get(header.kind, header.kind)
    if header.game_version:
        return f"{label} {header.game_version}"
    return f"{label} (v{header.version})" if header.version else label


def _pretty_race(raw: str) -> str:
    """"NordRace" → "Nord". Bethesda stores the editor id, not a display name."""
    if not raw:
        return ""
    return raw[:-4] if raw.endswith("Race") and len(raw) > 4 else raw


def _sex_text(widget, raw: str) -> str:
    if raw == "Male":
        return widget.tr("Male")
    if raw == "Female":
        return widget.tr("Female")
    return ""
