"""Nexus Collections browser - a full detachable tab.

Like the Nexus mods browser (gui_qt/nexus_browser_view.py) but simpler: no
category side-panel - just a top toolbar, a middle card grid, and a bottom bar
(search + pagination). Cards are VIEW-ONLY this pass (open the collection's Nexus
page); the install/detail flow is a separate feature.

  ┌───────────────────────────────────────────────┐
  │ Collections            Open on Nexus   Refresh │  toolbar
  ├───────────────────────────────────────────────┤
  │                 card grid                      │
  ├───────────────────────────────────────────────┤
  │ [search……] Search   ◂ Prev  Next ▸  page  … │  footer
  └───────────────────────────────────────────────┘

Collection data comes from the neutral Nexus/ layer (get_collections /
search_collections). Fetches run on a worker thread → _results_ready signal.
"""

from __future__ import annotations

import threading

from PySide6.QtCore import Qt, QTimer, Signal, QEvent
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QLabel, QLineEdit,
    QScrollArea, QFrame, QToolButton, QMenu, QCheckBox,
)

from gui_qt.theme_qt import active_palette, _c
from gui_qt.safe_emit import safe_emit
from gui_qt.nexus_mod_card import ThumbnailLoader
from gui_qt.collection_card import CollectionCard, CARD_W, IMG_W, IMG_H
from gui_qt.selector_button import SelectorButton
from gui_qt.mouse_navigation import MouseNavigationFilter

PAGE_SIZE = 20            # Tk parity (collections_dialog PAGE_SIZE)

# label → API sort key (see NexusAPI.COLLECTION_SORTS)
SORT_KEYS = [
    ("Most downloaded", "downloads"),
    ("Most endorsed", "endorsements"),
    ("Highest rated", "rating"),
    ("Recently listed", "recent"),
]


class CollectionsBrowserView(QWidget):
    """Required: *api* (authed NexusAPI), *domain* (game.nexus_game_domain),
    *game*. Optional *log_fn*."""

    _results_ready = Signal(object, str, object)   # (entries, status, token)

    def __init__(self, api, domain, game, log_fn=None, on_open_detail=None,
                 get_profile_dir=None, on_remove_appended=None,
                 parent=None):
        super().__init__(parent)
        self._api = api
        self._domain = domain or ""
        self._game = game
        self._log = log_fn or (lambda m: None)
        # Card View opens the detail tab when provided; else falls back to the URL.
        self._on_open_detail = on_open_detail
        # Appended-collections section: current profile dir provider + remove cb.
        self._get_profile_dir = get_profile_dir
        self._on_remove_appended = on_remove_appended

        # state
        self._show_adult = self._load_show_adult()
        self._page = 0
        self._query = ""
        self._sort = "downloads"
        self._entries = []
        self._cards: list[CollectionCard] = []
        self._appended_cards: list[CollectionCard] = []
        self._cols = 0
        self._fetch_token = 0           # guards against stale async results

        # Collection tiles are PORTRAIT - crop to the portrait cover size.
        self._thumbs = ThumbnailLoader(self, crop_w=IMG_W, crop_h=IMG_H)
        self._thumbs.loaded.connect(self._on_thumb)
        self._results_ready.connect(self._on_results)

        self._build()
        self._mouse_navigation = MouseNavigationFilter(
            self, self._prev_page, self._next_page)
        self._reload()

    # -- construction -------------------------------------------------------
    def _build(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        p = active_palette()

        # --- toolbar --------------------------------------------------------
        toolbar = QWidget()
        toolbar.setObjectName("HeaderBar")
        tb = QHBoxLayout(toolbar)
        tb.setContentsMargins(10, 6, 10, 6)
        tb.setSpacing(6)

        title = QLabel(self.tr("Collections"))
        title.setStyleSheet(f"color:{_c(p,'TEXT_MAIN')}; font-weight:600;")
        tb.addWidget(title)
        tb.addStretch(1)

        self._sort_sel = SelectorButton(
            items=[self.tr(lbl) for lbl, _ in SORT_KEYS],
            current=self.tr("Most downloaded"),
            prefix=self.tr("Sort: "), min_width=170,
            on_select=self._on_sort_changed)
        tb.addWidget(self._sort_sel)

        self._adult_cb = QCheckBox(self.tr("Show adult"))
        self._adult_cb.setChecked(self._show_adult)
        self._adult_cb.toggled.connect(self._on_adult_toggled)
        tb.addWidget(self._adult_cb)

        open_btn = QToolButton()
        open_btn.setText(self.tr("Open on Nexus"))
        open_btn.setObjectName("ActionButton")
        open_btn.setCursor(Qt.PointingHandCursor)
        open_btn.clicked.connect(self._open_game_collections_on_nexus)
        tb.addWidget(open_btn)

        refresh = QToolButton()
        refresh.setText(self.tr("Refresh"))
        refresh.setObjectName("ActionButton")
        refresh.setCursor(Qt.PointingHandCursor)
        refresh.clicked.connect(self._reload)
        tb.addWidget(refresh)

        outer.addWidget(toolbar)

        # --- card grid (no categories panel) --------------------------------
        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.NoFrame)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)

        # "Collections appended to this profile" - records from the current
        # profile's installed_collections/ folder; hidden when there are none.
        self._appended_host = QWidget()
        av = QVBoxLayout(self._appended_host)
        av.setContentsMargins(16, 12, 16, 0)
        av.setSpacing(6)
        appended_title = QLabel(self.tr("Collections appended to this profile"))
        appended_title.setStyleSheet(
            f"color:{_c(p,'TEXT_MAIN')}; font-weight:600;")
        av.addWidget(appended_title)
        appended_grid_host = QWidget()
        self._appended_grid = QGridLayout(appended_grid_host)
        self._appended_grid.setContentsMargins(0, 0, 0, 0)
        self._appended_grid.setSpacing(12)
        self._appended_grid.setAlignment(Qt.AlignTop)
        av.addWidget(appended_grid_host)
        sep = QFrame()
        sep.setFrameShape(QFrame.HLine)
        sep.setStyleSheet(f"color:{_c(p,'BORDER')};")
        av.addWidget(sep)
        self._appended_host.hide()

        self._grid_host = QWidget()
        self._grid = QGridLayout(self._grid_host)
        self._grid.setContentsMargins(16, 12, 16, 12)
        self._grid.setSpacing(12)
        self._grid.setAlignment(Qt.AlignTop)

        scroll_body = QWidget()
        sb = QVBoxLayout(scroll_body)
        sb.setContentsMargins(0, 0, 0, 0)
        sb.setSpacing(0)
        sb.addWidget(self._appended_host)
        sb.addWidget(self._grid_host)
        sb.addStretch(1)
        self._scroll.setWidget(scroll_body)
        self._scroll.installEventFilter(self)
        from gui_qt.loading_overlay import LoadingOverlay
        self._loading_overlay = LoadingOverlay(self._scroll)
        outer.addWidget(self._scroll, 1)

        # --- footer ---------------------------------------------------------
        footer = QWidget()
        footer.setObjectName("HeaderBar")
        ft = QHBoxLayout(footer)
        ft.setContentsMargins(10, 6, 10, 6)
        ft.setSpacing(6)

        self._search = QLineEdit()
        self._search.setPlaceholderText(self.tr("Search collections…"))
        self._search.setClearButtonEnabled(True)
        self._search.setFixedWidth(280)
        self._search.textChanged.connect(self._on_search_text)
        self._search.returnPressed.connect(self._do_search_now)
        ft.addWidget(self._search)
        sbtn = QToolButton()
        sbtn.setText(self.tr("Search"))
        sbtn.setObjectName("ActionButton")
        sbtn.setCursor(Qt.PointingHandCursor)
        sbtn.clicked.connect(self._do_search_now)
        ft.addWidget(sbtn)

        ft.addStretch(1)

        self._prev_btn = QToolButton()
        self._prev_btn.setText(self.tr("◂ Prev"))
        self._prev_btn.setObjectName("ActionButton")
        self._prev_btn.setCursor(Qt.PointingHandCursor)
        self._prev_btn.clicked.connect(self._prev_page)
        ft.addWidget(self._prev_btn)
        self._next_btn = QToolButton()
        self._next_btn.setText(self.tr("Next ▸"))
        self._next_btn.setObjectName("ActionButton")
        self._next_btn.setCursor(Qt.PointingHandCursor)
        self._next_btn.clicked.connect(self._next_page)
        ft.addWidget(self._next_btn)

        ft.addWidget(QLabel(self.tr("Page")))
        self._page_edit = QLineEdit()
        self._page_edit.setFixedWidth(48)
        self._page_edit.setAlignment(Qt.AlignCenter)
        self._page_edit.returnPressed.connect(self._jump_to_page)
        ft.addWidget(self._page_edit)

        self._status = QLabel("")
        self._status.setStyleSheet(f"color:{_c(p,'TEXT_DIM')};")
        ft.addWidget(self._status)

        outer.addWidget(footer)

    # -- adult filter -------------------------------------------------------
    @staticmethod
    def _load_show_adult() -> bool:
        try:
            from Utils.ui_config import load_nexus_show_adult
            return bool(load_nexus_show_adult())
        except Exception:
            return False

    def _on_adult_toggled(self, on: bool):
        self._show_adult = bool(on)
        try:
            from Utils.ui_config import save_nexus_show_adult
            save_nexus_show_adult(self._show_adult)
        except Exception:
            pass
        self._rebuild_cards()       # filter is applied at card-build time

    def _visible_entries(self):
        if self._show_adult:
            return self._entries
        return [e for e in self._entries
                if not getattr(e, "contains_adult_content", False)]

    # -- search -------------------------------------------------------------
    def _on_search_text(self, text: str):
        t = getattr(self, "_search_timer", None)
        if t is None:
            t = QTimer(self)
            t.setSingleShot(True)
            t.setInterval(300)
            t.timeout.connect(self._do_search_now)
            self._search_timer = t
        t.start()

    def _do_search_now(self):
        q = self._search.text().strip()
        if q == self._query:
            return
        self._query = q
        self._page = 0
        self._reload()

    # -- sort ---------------------------------------------------------------
    def _on_sort_changed(self, label: str):
        # Map the (possibly translated) menu label back to its API sort key.
        key = next((k for lbl, k in SORT_KEYS if self.tr(lbl) == label), None)
        if key is None:
            key = dict(SORT_KEYS).get(label, "downloads")
        if key == self._sort:
            return
        self._sort = key
        self._page = 0
        self._reload()

    # -- pagination ---------------------------------------------------------
    def _page_size(self) -> int:
        return PAGE_SIZE

    def _prev_page(self):
        if self._page > 0:
            self._page -= 1
            self._reload()

    def _next_page(self):
        # Only allow next when the last page filled (more likely exist).
        if len(self._entries) >= self._page_size():
            self._page += 1
            self._reload()

    def _jump_to_page(self):
        txt = self._page_edit.text().strip()
        if txt.isdigit():
            self._page = max(0, int(txt) - 1)
            self._reload()

    # -- fetch --------------------------------------------------------------
    def set_game(self, game, domain):
        """Retarget this browser at a different game (game switched while the tab
        is open). Resets paging + search and re-fetches the collection grid for
        the new domain."""
        self._game = game
        self._domain = domain or ""
        self._page = 0
        self._query = ""
        self._fetch_token += 1              # invalidate any in-flight fetch
        try:
            self._search.blockSignals(True)
            self._search.clear()
            self._search.blockSignals(False)
        except Exception:
            pass
        self._reload()

    def _reload(self):
        self.refresh_appended()
        if not self._domain:
            self._status.setText(self.tr("No Nexus domain for this game."))
            return
        self._fetch_token += 1
        token = self._fetch_token
        self._set_loading(True)
        page = self._page
        size = self._page_size()
        query = self._query
        domain = self._domain
        sort = self._sort

        def worker():
            entries = []
            status = ""
            try:
                if query:
                    entries = self._api.search_collections(
                        domain, query, count=size, offset=page * size, sort=sort)
                    status = self.tr("Search '{0}': page {1} "
                                     "({2} result(s))").format(
                                         query, page + 1, len(entries))
                else:
                    entries = self._api.get_collections(
                        domain, count=size, offset=page * size, sort=sort)
                    status = self.tr("Collections: page {0}").format(page + 1)
                if not entries and page == 0:
                    status = (self.tr("No collections found.")
                              if not query
                              else self.tr("No matches for '{0}'.").format(query))
            except Exception as exc:
                self._log(f"Nexus: collections error: {exc}")
                status = self.tr("Error: {0}").format(exc)
                entries = []
            safe_emit(self._results_ready, entries, status, token)

        threading.Thread(target=worker, daemon=True).start()

    def _on_results(self, entries, status, token):
        if token != self._fetch_token:
            return                       # stale
        self._entries = list(entries or [])
        self._status.setText(status)
        self._page_edit.setText(str(self._page + 1))
        self._set_loading(False)
        self._rebuild_cards()
        self._scroll.verticalScrollBar().setValue(0)
        self._update_page_buttons()

    def _set_loading(self, on: bool):
        for w in (self._prev_btn, self._next_btn, self._page_edit,
                  self._sort_sel):
            w.setEnabled(not on)
        if on:
            self._status.setText(self.tr("Loading…"))
            self._loading_overlay.show_over()
        else:
            self._loading_overlay.hide_overlay()

    def _update_page_buttons(self):
        self._prev_btn.setEnabled(self._page > 0)
        # Both browse and search page server-side via count/offset, so a full
        # page implies there may be a next one.
        self._next_btn.setEnabled(len(self._entries) >= self._page_size())

    # -- appended-collections section ----------------------------------------
    def refresh_appended(self):
        """Rebuild the 'Collections appended to this profile' section from the
        current profile's installed_collections/ records (hidden when empty)."""
        for c in self._appended_cards:
            c.setParent(None)
        self._appended_cards.clear()
        records = []
        pdir = None
        if self._get_profile_dir is not None:
            try:
                pdir = self._get_profile_dir()
            except Exception:
                pdir = None
        if pdir:
            from Utils.installed_collections import list_appended_collections
            records = list_appended_collections(pdir, log_fn=self._log)
        if not records:
            self._appended_host.hide()
            return
        import dataclasses
        from Nexus.nexus_api import NexusCollection
        fields = {f.name for f in dataclasses.fields(NexusCollection)}
        primary_view = self._on_open_detail or self._on_view
        for rec in records:
            data = {k: v for k, v in (rec.get("card") or {}).items()
                    if k in fields and v is not None}
            try:
                entry = NexusCollection(**data)
            except Exception:
                entry = NexusCollection()
            if not entry.slug:
                entry.slug = str(rec.get("slug") or "")
            if not entry.name:
                entry.name = entry.slug
            on_remove = None
            if self._on_remove_appended is not None:
                on_remove = lambda _e, rec=rec: self._on_remove_appended(rec)
            card = CollectionCard(entry, primary_view,
                                  on_context=self._show_card_menu,
                                  on_remove=on_remove)
            self._appended_cards.append(card)
            if entry.tile_image_url:
                self._thumbs.request(entry.id, entry.tile_image_url)
        self._appended_host.show()
        self._relayout_appended(self._cols_for_width())

    def _relayout_appended(self, cols: int):
        while self._appended_grid.count():
            self._appended_grid.takeAt(0)
        for i, card in enumerate(self._appended_cards):
            self._appended_grid.addWidget(card, i // cols, 1 + (i % cols),
                                          Qt.AlignTop | Qt.AlignHCenter)
            card.show()
        for c in range(self._appended_grid.columnCount()):
            self._appended_grid.setColumnStretch(c, 0)
        self._appended_grid.setColumnStretch(0, 1)
        self._appended_grid.setColumnStretch(cols + 1, 1)

    # -- cards / grid -------------------------------------------------------
    def _rebuild_cards(self):
        for c in self._cards:
            c.setParent(None)
        self._cards.clear()
        primary_view = self._on_open_detail or self._on_view
        for e in self._visible_entries():
            card = CollectionCard(e, primary_view,
                                  on_context=self._show_card_menu)
            self._cards.append(card)
            self._thumbs.request(e.id, getattr(e, "tile_image_url", "") or "")
        self._cols = 0
        self._relayout()

    def _cols_for_width(self) -> int:
        vp = self._scroll.viewport().width()
        slot = CARD_W + self._grid.spacing()
        return max(1, (vp - 32) // slot)

    def _relayout(self):
        cols = self._cols_for_width()
        while self._grid.count():
            self._grid.takeAt(0)
        # Center the row group: cards live in columns 1..cols, with equal-stretch
        # spacer columns on both sides (0 and cols+1).
        for i, card in enumerate(self._cards):
            self._grid.addWidget(card, i // cols, 1 + (i % cols),
                                 Qt.AlignTop | Qt.AlignHCenter)
            card.show()
        for c in range(self._grid.columnCount()):
            self._grid.setColumnStretch(c, 0)
        self._grid.setColumnStretch(0, 1)
        self._grid.setColumnStretch(cols + 1, 1)
        if self._appended_cards:
            self._relayout_appended(cols)
        self._cols = cols

    def _on_thumb(self, coll_id, pm):
        for card in self._cards + self._appended_cards:
            if card.entry.id == coll_id:
                card.set_thumbnail(pm)

    def eventFilter(self, obj, event):
        if obj is self._scroll and event.type() == QEvent.Resize:
            if self._cols_for_width() != self._cols:
                self._relayout()
        return super().eventFilter(obj, event)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self._cols_for_width() != self._cols:
            self._relayout()

    # -- card actions -------------------------------------------------------
    def _collection_url(self, entry) -> str:
        dom = getattr(entry, "game_domain", "") or self._domain
        return f"https://www.nexusmods.com/games/{dom}/collections/{entry.slug}"

    def _on_view(self, entry):
        from Utils.xdg import open_url
        open_url(self._collection_url(entry), log_fn=self._log)

    def _open_game_collections_on_nexus(self):
        from Utils.xdg import open_url
        open_url(f"https://www.nexusmods.com/games/{self._domain}/collections",
                 log_fn=self._log)

    def _show_card_menu(self, entry, global_pos):
        menu = QMenu(self)
        menu.addAction(self.tr("Open on Nexus"), lambda: self._on_view(entry))
        menu.exec(global_pos)
