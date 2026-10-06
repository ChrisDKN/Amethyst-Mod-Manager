"""Add-Game view - a reflowing card grid for selecting/adding games.

Qt port of gui/game_picker_dialog.py (v1 scope). Each card shows the game's
square logo, its name, and a button: "Select" (green) if the game is already
configured, else "Add" (blue accent) to configure it. A search box filters by
name.

The grid is split into two sections: "Installed" games (detected via supported
launchers) on top, and "Not Installed" games below, so users can quickly find
the games they actually own. A "Show only installed" checkbox hides the bottom
section entirely.

The view embeds as a detachable tab. It calls back:
    on_select(game_name)  - a configured game was picked (switch to it)
    on_add(game_name)     - an unconfigured game was picked (start configure)
    on_configure(game_name) - configure a game without switching to it
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, Signal, QSize
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QLabel, QLineEdit,
    QScrollArea, QPushButton, QFrame, QCheckBox, QToolButton,
)

from gui_qt.theme_qt import active_palette, bind_theme_icon, _c
from gui_qt.icons import icon

_ICONS_DIR = Path(__file__).resolve().parent.parent / "icons" / "games"
_LAUNCHER_NAMES = {
    "steam": "Steam", "heroic": "Heroic", "lutris": "Lutris",
    "faugus": "Faugus", "bottles": "Bottles",
}

CARD_W = 180
CARD_H = 210
IMG_SQ = 140


def _game_logo(game_id: str, size: int) -> QPixmap | None:
    """Load <game_id>.png (or lowercase / custom-images variant), center-crop to
    a square and scale to *size*. Returns None if no logo file exists."""
    candidates = [_ICONS_DIR / f"{game_id}.png", _ICONS_DIR / f"{game_id.lower()}.png"]
    try:
        from Utils.config_paths import get_custom_game_images_dir
        candidates.append(get_custom_game_images_dir() / f"{game_id}.png")
    except Exception:
        pass
    path = next((p for p in candidates if p.is_file()), None)
    if path is None:
        return None
    pm = QPixmap(str(path))
    if pm.isNull():
        return None
    # Center-crop to square (cover), then scale.
    side = min(pm.width(), pm.height())
    x = (pm.width() - side) // 2
    y = (pm.height() - side) // 2
    pm = pm.copy(x, y, side, side)
    return pm.scaled(size, size, Qt.KeepAspectRatio, Qt.SmoothTransformation)


class _GameCard(QFrame):
    def __init__(self, name: str, game, on_select, on_add, on_unavailable,
                 on_configure, parent=None):
        super().__init__(parent)
        self.setObjectName("GameCard")
        self.setFixedSize(CARD_W, CARD_H)
        self._name = name
        self._launcher_badges: list[QLabel] = []
        configured = bool(game and game.is_configured())
        missing = (game.missing_configured_paths()
                   if game is not None and not configured else [])
        game_id = (getattr(game, "game_id", None)
                   or name.lower().replace(" ", "_"))
        self._game_id = game_id

        v = QVBoxLayout(self)
        v.setContentsMargins(8, 8, 8, 8)
        v.setSpacing(6)

        # Logo (or a "?" placeholder).
        self._logo = QLabel()
        self._logo.setAlignment(Qt.AlignCenter)
        self._logo.setFixedHeight(IMG_SQ)
        self.reload_logo()
        v.addWidget(self._logo)

        # Name (wraps, centered).
        title = QLabel(name)
        title.setAlignment(Qt.AlignCenter)
        title.setWordWrap(True)
        title.setObjectName("GameCardName")
        v.addWidget(title, 1)

        label = (self.tr("Select") if configured else
                 self.tr("Unavailable") if missing else self.tr("Add"))
        btn = QPushButton(label)
        btn.setCursor(Qt.PointingHandCursor)
        btn.setObjectName("GameSelectBtn" if configured else "GameAddBtn")
        if missing:
            btn.setToolTip(self.tr("Saved game unavailable. Check these locations:\n{0}").format(
                "\n".join(str(path) for path in missing)))
        btn.clicked.connect(
            (lambda: on_select(name)) if configured else
            (lambda: on_unavailable(name)) if missing else
            (lambda: on_add(name)))
        actions = QHBoxLayout()
        actions.setSpacing(4)
        actions.addWidget(btn, 1)
        if configured:
            configure = QToolButton()
            configure.setObjectName("IconButton")
            configure.setFixedSize(28, 28)
            configure.setStyleSheet("padding:0;")
            configure.setIconSize(QSize(18, 18))
            bind_theme_icon(configure, "settings.png", 18, "TEXT_MAIN")
            configure.setToolTip(self.tr("Configure game"))
            configure.setAccessibleName(self.tr("Configure {0}").format(name))
            configure.setCursor(Qt.PointingHandCursor)
            configure.clicked.connect(lambda: on_configure(name))
            actions.addWidget(configure)
        v.addLayout(actions)

    def set_launchers(self, launchers: tuple[str, ...]) -> None:
        for badge in self._launcher_badges:
            badge.hide()
            badge.deleteLater()
        self._launcher_badges.clear()
        palette = active_palette()
        for source, name in _LAUNCHER_NAMES.items():
            if source not in launchers:
                continue
            badge = QLabel(self)
            badge.setFixedSize(28, 28)
            badge.setAlignment(Qt.AlignCenter)
            badge.setStyleSheet(
                f"background:{_c(palette, 'BG_PANEL')};"
                f"border:1px solid {_c(palette, 'BORDER')};"
                "border-radius:6px; padding:0;")
            badge.setPixmap(icon(f"{source}.png", 20).pixmap(20, 20))
            badge.setToolTip(name)
            badge.setAccessibleName(name)
            badge.move(8 + len(self._launcher_badges) * 32, 8)
            badge.show()
            badge.raise_()
            self._launcher_badges.append(badge)

    def reload_logo(self) -> None:
        """(Re)load the logo pixmap from disk, falling back to a '?' placeholder.
        Called once at build and again when a background image download lands."""
        pm = _game_logo(self._game_id, IMG_SQ)
        if pm is not None:
            self._logo.setText("")
            self._logo.setStyleSheet("")
            self._logo.setPixmap(pm)
        else:
            self._logo.setPixmap(QPixmap())
            self._logo.setText("?")
            self._logo.setStyleSheet(
                f"color:{_c(active_palette(),'TEXT_DIM')}; font-size:36px;"
                " font-weight:bold;")


class AddGameView(QWidget):
    """The reflowing card grid. *on_select(name)* / *on_add(name)* are required."""

    # Emitted from the scan worker with each installed game's launchers.
    _installed_scanned = Signal(dict)

    # game_id whose freshly-downloaded logo just landed → refresh its card.
    _image_ready = Signal(str)

    def __init__(self, games: dict, on_select, on_add, on_unavailable,
                 on_configure, parent=None):
        super().__init__(parent)
        self._games = games
        self._on_select = on_select
        self._on_add = on_add
        self._on_unavailable = on_unavailable
        self._on_configure = on_configure
        self._cards: list[tuple[str, _GameCard]] = []   # (search_text, card)
        self._cols = 0
        # Installed-only filter state. None = not yet scanned.
        self._installed_game_names: set[str] | None = None
        self._installed_scanned.connect(self._on_installed_scanned)
        self._image_ready.connect(self._on_image_ready)
        self._build()
        self._populate()

    def refresh_games(self, games: dict) -> None:
        """Rebuild the card grid from *games* in place (e.g. after a background
        handler sync downloaded new definitions). Keeps the view alive so no
        running scan/image thread emits on a deleted signal."""
        self._games = games
        for _s, card in self._cards:
            card.setParent(None)
            card.deleteLater()
        self._cards.clear()
        self._installed_game_names = None
        self._populate()

    def on_image_downloaded(self, game_id: str) -> None:
        """Worker-thread callback from download_missing_custom_game_images -
        marshal to the GUI thread to refresh the matching card's logo."""
        from gui_qt.safe_emit import safe_emit
        safe_emit(self._image_ready, game_id)

    def _on_image_ready(self, game_id: str) -> None:
        for _s, card in self._cards:
            if getattr(card, "_game_id", "") == game_id:
                card.reload_logo()
                break

    def _build(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # Header: title + search.
        header = QWidget()
        header.setObjectName("HeaderBar")
        hb = QHBoxLayout(header)
        hb.setContentsMargins(12, 8, 12, 8)
        title = QLabel(self.tr("Select a game to add"))
        title.setStyleSheet("font-size:15px; font-weight:600;")
        hb.addWidget(title)
        hb.addStretch(1)
        self._installed_cb = QCheckBox(self.tr("Show only installed"))
        self._installed_cb.setCursor(Qt.PointingHandCursor)
        self._installed_cb.toggled.connect(self._on_installed_filter_toggle)
        hb.addWidget(self._installed_cb)
        self._search = QLineEdit()
        self._search.setPlaceholderText(self.tr("Search by name…"))
        self._search.setClearButtonEnabled(True)
        self._search.setFixedWidth(260)
        self._search.textChanged.connect(self._apply_search)
        hb.addWidget(self._search)
        outer.addWidget(header)

        # Scroll area holding the grid. Cards reflow to fit the width, so the
        # horizontal scrollbar is never needed (it only appears when the grid is
        # momentarily wider than the viewport - disable it outright).
        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.NoFrame)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._grid_host = QWidget()
        self._grid = QGridLayout(self._grid_host)
        self._grid.setContentsMargins(16, 12, 16, 12)
        self._grid.setSpacing(12)
        self._grid.setAlignment(Qt.AlignTop)
        self._scroll.setWidget(self._grid_host)
        # Reflow when the scroll area itself resizes (covers detach into a
        # narrower/wider window, not just the outer view's resizeEvent).
        self._scroll.installEventFilter(self)
        from gui_qt.loading_overlay import LoadingOverlay
        self._loading_overlay = LoadingOverlay(self._scroll)
        outer.addWidget(self._scroll, 1)

    def eventFilter(self, obj, event):
        from PySide6.QtCore import QEvent
        if obj is self._scroll and event.type() == QEvent.Resize:
            slot = CARD_W + self._grid.spacing()
            cols = max(1, (self._scroll.viewport().width() - 32) // slot)
            if cols != self._cols:
                self._relayout()
        return super().eventFilter(obj, event)

    def _populate(self):
        for name in sorted(self._games, key=str.lower):
            game = self._games[name]
            card = _GameCard(name, game, self._on_select, self._on_add,
                             self._on_unavailable, self._on_configure)
            search = self._search_text(name, game)
            self._cards.append((search, card))
        self._relayout()
        # Detect installed games up front so we can split into sections. Runs on
        # a worker thread; results marshaled back via _installed_scanned. Show a
        # spinner over the grid while the launcher scan runs.
        self._loading_overlay.show_over()
        import threading
        threading.Thread(target=self._scan_installed_games, daemon=True).start()

    @staticmethod
    def _search_text(name: str, game) -> str:
        parts = [name]
        if game is not None:
            for attr in ("game_id", "steam_id"):
                val = getattr(game, attr, None)
                if val:
                    parts.append(str(val))
        return " ".join(parts).lower()

    def _matching_cards(self) -> list[_GameCard]:
        """Cards matching the current search query (section split happens later)."""
        q = self._search.text().strip().lower() if hasattr(self, "_search") else ""
        return [c for s, c in self._cards if not q or q in s]

    # ------------------------------------------------------------------
    # Installed-only filter
    # ------------------------------------------------------------------

    def _on_installed_filter_toggle(self, _checked: bool):
        # The scan runs on load, so toggling just re-lays out the sections
        # (hiding / showing the "Not Installed" section).
        self._relayout()

    def _scan_installed_games(self):
        """Collect launcher matches using one shared index in a worker thread."""
        from gui_qt.safe_emit import safe_emit

        def _log(msg: str) -> None:
            try:
                from Utils.app_log import app_log
                app_log(f"add-game scan: {msg}")
            except Exception:
                pass

        installed: dict[str, tuple[str, ...]] = {}
        # Outer guard: no matter what fails below, we always emit the result so
        # the loading overlay is cleared and the user can still add games.
        try:
            import time
            t0 = time.monotonic()
            try:
                from Utils.launchers.installed import InstalledIndex
                index = InstalledIndex(log=_log)
            except Exception as exc:
                import traceback
                _log(f"index build failed, skipping detection: {exc}\n"
                     f"{traceback.format_exc()}")
                safe_emit(self._installed_scanned, {})
                return

            for name, game in self._games.items():
                # Per-game guard: one malformed game definition (e.g. a synced
                # custom handler missing exe_name) must not abort the whole scan.
                try:
                    launchers = index.game_launchers(game)
                except Exception as exc:
                    import traceback
                    _log(f"detection failed for game '{name}', treating as "
                         f"not installed: {exc}\n{traceback.format_exc()}")
                    launchers = ()
                if launchers:
                    installed[name] = launchers
            _log(f"scanned {len(self._games)} games in "
                 f"{time.monotonic() - t0:.2f}s - {len(installed)} installed")
        except Exception as exc:
            import traceback
            _log(f"scan aborted unexpectedly: {exc}\n{traceback.format_exc()}")

        safe_emit(self._installed_scanned, installed)

    def _on_installed_scanned(self, installed: dict):
        self._installed_game_names = set(installed)
        for _search, card in self._cards:
            card.set_launchers(installed.get(card._name, ()))
        self._loading_overlay.hide_overlay()
        self._relayout()

    def _cols_for_width(self) -> int:
        vp_w = self._scroll.viewport().width()
        slot = CARD_W + self._grid.spacing()
        return max(1, (vp_w - 32) // slot)

    def _make_section_header(self, text: str) -> QLabel:
        lbl = QLabel(text)
        lbl.setObjectName("GameSectionHeader")
        lbl.setStyleSheet(
            f"color:{_c(active_palette(),'TEXT_DIM')}; font-size:13px;"
            " font-weight:600; padding:4px 0;")
        return lbl

    def _add_card_run(self, cards: list[_GameCard], cols: int, start_row: int) -> int:
        """Place *cards* into the grid starting at *start_row*. Cards occupy
        columns 1..cols; columns 0 and cols+1 are equal-weight spacers so the
        block is centered. Returns the next free row."""
        for i, card in enumerate(cards):
            self._grid.addWidget(
                card, start_row + i // cols, 1 + (i % cols),
                Qt.AlignTop | Qt.AlignHCenter)
            card.show()
        rows = (len(cards) + cols - 1) // cols
        return start_row + max(rows, 0)

    def _relayout(self):
        cols = self._cols_for_width()
        # Clear the grid (without deleting the cards) and drop old section
        # headers (those are owned by the grid, so deleteLater them).
        while self._grid.count():
            item = self._grid.takeAt(0)
            w = item.widget()
            if isinstance(w, QLabel):   # section header (cards are _GameCard)
                w.deleteLater()
        for c in self._cards:
            c[1].hide()

        matching = self._matching_cards()
        scanned = self._installed_game_names is not None
        show_only_installed = self._installed_cb.isChecked()

        if not scanned:
            # Scan not finished yet - render everything flat (no headers).
            row = self._add_card_run(matching, cols, 0)
        else:
            installed = [c for c in matching if c._name in self._installed_game_names]
            not_installed = [c for c in matching
                             if c._name not in self._installed_game_names]
            row = 0
            # Only show the "Installed" header when the other section is present
            # too (otherwise a lone header is just noise).
            show_headers = bool(installed) and bool(not_installed) \
                and not show_only_installed
            if installed:
                if show_headers:
                    self._grid.addWidget(
                        self._make_section_header(self.tr("Installed")), row, 1, 1, cols)
                    row += 1
                row = self._add_card_run(installed, cols, row)
            if not_installed and not show_only_installed:
                if show_headers:
                    self._grid.addWidget(
                        self._make_section_header(self.tr("Not Installed")), row, 1, 1, cols)
                    row += 1
                row = self._add_card_run(not_installed, cols, row)

        for c in range(self._grid.columnCount()):
            self._grid.setColumnStretch(c, 0)
        self._grid.setColumnStretch(0, 1)
        self._grid.setColumnStretch(cols + 1, 1)
        self._cols = cols

    def _apply_search(self, _text=None):
        self._relayout()

    def showEvent(self, event):
        super().showEvent(event)
        # Reparented (e.g. detached into a new window) → the viewport width may
        # have changed; reflow once the geometry settles.
        from PySide6.QtCore import QTimer
        QTimer.singleShot(0, self._relayout)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self._cols_for_width() != self._cols:
            self._relayout()
