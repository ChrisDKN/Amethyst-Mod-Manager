"""Thunderstore browser - a full detachable tab.

Structurally a sibling of gui_qt/nexus_browser_view.py (the house convention is
"clone the view, share the leaf widgets"; CollectionsBrowserView is itself such
a clone). Layout:

  ┌────────────────────────────────────────────────────────────┐
  │ ☰ Categories [Section▾][Sort▾] ☐Deprecated ☐NSFW  Refresh │  toolbar
  ├──────────┬─────────────────────────────────────────────────┤
  │ Categories│                card grid                        │
  ├──────────┴─────────────────────────────────────────────────┤
  │ [search……] Search ✕   ◂ Prev  Next ▸  page [ ]/N    status │  footer
  └────────────────────────────────────────────────────────────┘

All data comes from the toolkit-neutral Thunderstore/thunderstore_api.py; this
file is pure Qt UI + threading. It is markedly simpler than the Nexus browser
because Thunderstore needs no auth: there is no premium/free split, no expiring
links, no per-file chooser and no manual-download watching. One package version
is one zip.

Cards are `NexusModCard` - it is duck-typed on a plain entry object, so
`_CardEntry` below adapts the listing JSON onto the attribute names it reads.
"""

from __future__ import annotations

import hashlib
import threading
from dataclasses import dataclass

from PySide6.QtCore import Qt, QTimer, Signal, QEvent
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QLabel, QLineEdit,
    QScrollArea, QFrame, QCheckBox, QToolButton, QSplitter,
)

from Thunderstore.thunderstore_api import (
    ORDERINGS, fetch_filters, fetch_latest_version, fetch_listing, package_url,
    total_pages)
from gui_qt.flow_layout import FlowLayout
from gui_qt.mouse_navigation import MouseNavigationFilter
from gui_qt.nexus_mod_card import CARD_W, NexusModCard, ThumbnailLoader
from gui_qt.safe_emit import safe_emit
from gui_qt.selector_button import SelectorButton
from gui_qt.theme_qt import active_palette, _c
from gui_qt.worker import run_in_worker

_ALL_SECTIONS = "All sections"
_DEFAULT_ORDERING = ("Most downloaded", "most-downloaded")


def _synth_id(namespace: str, name: str) -> int:
    """A stable positive 31-bit id for a package.

    Thunderstore packages have no numeric id, but ThumbnailLoader keys its
    completion signal on ``int`` and Qt TRUNCATES anything wider than 32 bits
    (measured: 58252595840366036 arrives as 1649358292). blake2b over the
    lowercased ``{namespace}-{name}`` gives an id that is stable across pages
    and reloads, so a late-arriving thumbnail still finds its card.

    Used ONLY for thumbnail routing - install state is matched on package
    identity, so a theoretical hash collision would at worst paint one card the
    wrong icon, never mis-install anything.
    """
    key = f"{namespace}-{name}".lower().encode("utf-8")
    return int.from_bytes(hashlib.blake2b(key, digest_size=4).digest(),
                          "big") & 0x7FFFFFFF


@dataclass
class _CardEntry:
    """Adapts a ThunderstoreListing onto the attributes NexusModCard reads."""

    mod_id: int = 0
    name: str = ""
    author: str = ""                # ← namespace
    category_name: str = ""         # ← first category
    summary: str = ""               # ← description
    updated_at: str = ""            # ← last_updated (ISO)
    created_at: str = ""            # ← datetime_created (ISO)
    endorsement_count: int = 0      # ← rating_count (card paints "♥")
    downloads_total: int = 0        # ← download_count
    file_size_kb: int = 0           # ← size // 1024 (API gives BYTES)
    picture_url: str = ""           # ← icon_url
    # Thunderstore-side fields, read by the view (not by the card):
    namespace: str = ""
    package_name: str = ""
    is_deprecated: bool = False
    is_nsfw: bool = False
    community: str = ""

    @property
    def package_id(self) -> str:
        return f"{self.namespace}-{self.package_name}"


def _adapt(listing) -> _CardEntry:
    """ThunderstoreListing → _CardEntry."""
    cat = listing.categories[0][1] if listing.categories else ""
    return _CardEntry(
        mod_id=_synth_id(listing.namespace, listing.name),
        name=listing.name,
        author=listing.namespace,
        category_name=cat,
        summary=listing.description,
        updated_at=listing.last_updated,
        created_at=listing.datetime_created,
        endorsement_count=listing.rating_count,
        downloads_total=listing.download_count,
        # The API reports BYTES; _fmt_size_kb expects KB. Without this every
        # mod renders ~1000x too large.
        file_size_kb=int(listing.size or 0) // 1024,
        picture_url=listing.icon_url,
        namespace=listing.namespace,
        package_name=listing.name,
        is_deprecated=listing.is_deprecated,
        is_nsfw=listing.is_nsfw,
        community=listing.community,
    )


class ThunderstoreBrowserView(QWidget):
    """Required: *community* (game.thunderstore_community), *game*.
    Optional: *install_fn(namespace, name, version)*, *log_fn*."""

    # (entries, status, token, total_count)
    _results_ready = Signal(object, str, object, int)
    # (ThunderstoreFilters|None, community)
    _filters_ready = Signal(object, str)
    # (entry, version|"") - the latest version resolved for an install click
    _version_ready = Signal(object, str)

    def __init__(self, community, game, install_fn=None, log_fn=None,
                 parent=None):
        super().__init__(parent)
        self._community = (community or "").strip()
        self._game = game
        self._install_fn = install_fn or (lambda ns, name, ver: None)
        self._log = log_fn or (lambda m: None)

        # state
        self._page = 0                  # 0-based (the API is 1-based)
        self._ordering = _DEFAULT_ORDERING[1]
        self._section = ""              # section uuid ("" = all)
        self._sections: list = []       # [(uuid, name, slug, priority)]
        self._query = ""
        self._included_cats: list = []
        self._excluded_cats: list = []
        self._show_nsfw = self._load_flag("show_nsfw")
        self._show_deprecated = self._load_flag("show_deprecated")
        self._entries: list = []
        self._cards: list = []
        self._cols = 0
        self._total = 0
        self._fetch_token = 0           # guards against stale async results
        self._filters_loaded = False
        self._search_timer = None

        self._thumbs = ThumbnailLoader(self)
        self._thumbs.loaded.connect(self._on_thumb)
        self._results_ready.connect(self._on_results)
        self._filters_ready.connect(self._on_filters)
        self._version_ready.connect(self._on_version_ready)

        self._build()
        self._mouse_navigation = MouseNavigationFilter(
            self, self._prev_page, self._next_page)
        self._load_filters()
        self._reload()

    # -- persisted toggles --------------------------------------------------
    @staticmethod
    def _load_flag(key: str) -> bool:
        try:
            from Utils.ui_config import load_thunderstore_flag
            return bool(load_thunderstore_flag(key))
        except Exception:
            return False

    @staticmethod
    def _save_flag(key: str, value: bool) -> None:
        try:
            from Utils.ui_config import save_thunderstore_flag
            save_thunderstore_flag(key, bool(value))
        except Exception:
            pass

    @staticmethod
    def _filter_qss(p) -> str:
        """Match the modlist filter side-panel styling (same #Filter* QSS)."""
        c = lambda k: _c(p, k)
        return f"""
        #FilterPanel {{ background: {c('BG_PANEL')}; }}
        #FilterHeader {{ background: {c('BG_HEADER')}; }}
        #FilterTitle {{ font-weight: bold; font-size: 14px; color: {c('TEXT_MAIN')}; }}
        #FilterRule {{ background: {c('BORDER')}; }}
        #FilterBody {{ background: {c('BG_PANEL')}; }}
        #FilterEmpty {{ color: {c('TEXT_DIM')}; font-style: italic; }}
        QScrollArea {{ background: {c('BG_PANEL')}; border: none; }}
        """

    @staticmethod
    def _enable_hfw(w: QWidget) -> None:
        """Report the bar's wrapped FlowLayout height via heightForWidth."""
        pol = w.sizePolicy()
        pol.setHeightForWidth(True)
        w.setSizePolicy(pol)

    # -- build --------------------------------------------------------------
    def _build(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        p = active_palette()

        # ---- toolbar ------------------------------------------------------
        # FlowLayout, NOT QHBoxLayout: a non-wrapping row's minimum width
        # poisons the whole QStackedWidget's minimum when this tab is pinned
        # into a panel, jamming the body splitter for every other panel tab.
        toolbar = QWidget()
        toolbar.setObjectName("HeaderBar")
        tb = FlowLayout(toolbar, margin=6, spacing=6)
        tb.setContentsMargins(10, 6, 10, 6)
        self._enable_hfw(toolbar)

        self._cat_toggle = QToolButton()
        self._cat_toggle.setObjectName("ActionButton")
        self._cat_toggle.setText(self.tr("☰ Categories"))
        self._cat_toggle.setCheckable(True)
        self._cat_toggle.setChecked(True)
        self._cat_toggle.setCursor(Qt.PointingHandCursor)
        self._cat_toggle.toggled.connect(self._toggle_categories)
        tb.addWidget(self._cat_toggle)

        self._section_sel = SelectorButton(
            items=[_ALL_SECTIONS], current=_ALL_SECTIONS,
            prefix=self.tr("Section: "), min_width=170,
            on_select=self._on_section)
        tb.addWidget(self._section_sel)

        self._sort_sel = SelectorButton(
            items=[lbl for lbl, _v in ORDERINGS],
            current=_DEFAULT_ORDERING[0],
            prefix=self.tr("Sort: "), min_width=190, on_select=self._on_sort)
        tb.addWidget(self._sort_sel)

        tb.add_stretch()

        # Both are SERVER-side params (unlike the Nexus adult filter, which is
        # applied client-side), so toggling either must re-query, not re-paint.
        self._dep_cb = QCheckBox(self.tr("Deprecated"))
        self._dep_cb.setChecked(self._show_deprecated)
        self._dep_cb.toggled.connect(self._on_deprecated)
        tb.addWidget(self._dep_cb)

        self._nsfw_cb = QCheckBox(self.tr("NSFW"))
        self._nsfw_cb.setChecked(self._show_nsfw)
        self._nsfw_cb.toggled.connect(self._on_nsfw)
        tb.addWidget(self._nsfw_cb)

        refresh = QToolButton()
        refresh.setObjectName("ActionButton")
        refresh.setText(self.tr("Refresh"))
        refresh.setCursor(Qt.PointingHandCursor)
        refresh.clicked.connect(self._reload)
        tb.addWidget(refresh)

        outer.addWidget(toolbar)

        # ---- body ---------------------------------------------------------
        self._body_split = QSplitter(Qt.Horizontal)
        self._body_split.setHandleWidth(6)
        self._body_split.setChildrenCollapsible(True)

        self._cat_panel = QWidget()
        self._cat_panel.setObjectName("FilterPanel")
        self._cat_panel.setStyleSheet(self._filter_qss(p))
        self._cat_panel.setMinimumWidth(120)
        self._cat_panel.setMaximumWidth(460)
        cp = QVBoxLayout(self._cat_panel)
        cp.setContentsMargins(0, 0, 0, 0)
        cp.setSpacing(0)
        cat_header = QWidget()
        cat_header.setObjectName("FilterHeader")
        ch = QHBoxLayout(cat_header)
        ch.setContentsMargins(10, 6, 8, 6)
        ct = QLabel(self.tr("Categories"))
        ct.setObjectName("FilterTitle")
        ch.addWidget(ct)
        ch.addStretch(1)
        cp.addWidget(cat_header)
        rule = QFrame()
        rule.setObjectName("FilterRule")
        rule.setFixedHeight(1)
        cp.addWidget(rule)
        self._cat_scroll = QScrollArea()
        self._cat_scroll.setWidgetResizable(True)
        self._cat_scroll.setFrameShape(QFrame.NoFrame)
        self._cat_host = QWidget()
        self._cat_host.setObjectName("FilterBody")
        self._cat_layout = QVBoxLayout(self._cat_host)
        self._cat_layout.setContentsMargins(10, 8, 10, 12)
        self._cat_layout.setSpacing(3)
        self._cat_layout.setAlignment(Qt.AlignTop)
        self._cat_scroll.setWidget(self._cat_host)
        cp.addWidget(self._cat_scroll, 1)
        self._body_split.addWidget(self._cat_panel)

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
        self._scroll.installEventFilter(self)
        self._body_split.addWidget(self._scroll)
        # 3 cards at the 1280 minimum window width need ~968px of grid.
        self._body_split.setSizes([260, 1020])
        self._cat_width = 260
        outer.addWidget(self._body_split, 1)

        from gui_qt.loading_overlay import LoadingOverlay
        self._loading_overlay = LoadingOverlay(self._scroll)

        # ---- footer -------------------------------------------------------
        footer = QWidget()
        footer.setObjectName("HeaderBar")
        ft = FlowLayout(footer, margin=6, spacing=6)
        ft.setContentsMargins(10, 6, 10, 6)
        self._enable_hfw(footer)

        self._search = QLineEdit()
        self._search.setPlaceholderText(self.tr("Search mods…"))
        self._search.setClearButtonEnabled(True)
        self._search.setFixedWidth(280)
        self._search.textChanged.connect(self._on_search_text)
        self._search.returnPressed.connect(self._do_search_now)
        ft.addWidget(self._search)

        sbtn = QToolButton()
        sbtn.setObjectName("ActionButton")
        sbtn.setText(self.tr("Search"))
        sbtn.setCursor(Qt.PointingHandCursor)
        sbtn.clicked.connect(self._do_search_now)
        ft.addWidget(sbtn)

        cbtn = QToolButton()
        cbtn.setObjectName("ActionButton")
        cbtn.setText(self.tr("Clear"))
        cbtn.setCursor(Qt.PointingHandCursor)
        cbtn.clicked.connect(self._clear_search)
        ft.addWidget(cbtn)

        ft.add_stretch()

        self._prev_btn = QToolButton()
        self._prev_btn.setObjectName("ActionButton")
        self._prev_btn.setText(self.tr("◂ Prev"))
        self._prev_btn.setCursor(Qt.PointingHandCursor)
        self._prev_btn.clicked.connect(self._prev_page)
        ft.addWidget(self._prev_btn)

        self._next_btn = QToolButton()
        self._next_btn.setObjectName("ActionButton")
        self._next_btn.setText(self.tr("Next ▸"))
        self._next_btn.setCursor(Qt.PointingHandCursor)
        self._next_btn.clicked.connect(self._next_page)
        ft.addWidget(self._next_btn)

        ft.addWidget(QLabel(self.tr("Page")))
        self._page_edit = QLineEdit()
        self._page_edit.setFixedWidth(48)
        self._page_edit.setAlignment(Qt.AlignCenter)
        self._page_edit.returnPressed.connect(self._jump_to_page)
        ft.addWidget(self._page_edit)
        self._page_total = QLabel("")
        self._page_total.setStyleSheet(f"color:{_c(p,'TEXT_DIM')};")
        ft.addWidget(self._page_total)

        self._status = QLabel("")
        self._status.setStyleSheet(f"color:{_c(p,'TEXT_DIM')};")
        ft.addWidget(self._status)

        outer.addWidget(footer)

    # -- filters (categories + sections) ------------------------------------
    def _load_filters(self):
        if self._filters_loaded or not self._community:
            return
        community = self._community
        run_in_worker(lambda: (fetch_filters(community), community),
                      self._filters_ready, name="ts-filters", unpack=True,
                      error_result=(None, community))

    def _on_filters(self, filters, community):
        if community != self._community:
            return          # a retarget landed first
        self._filters_loaded = True
        # Sections dropdown.
        self._sections = list(getattr(filters, "sections", []) or [])
        labels = [_ALL_SECTIONS] + [s[1] for s in self._sections]
        self._section_sel.set_items(labels, current=_ALL_SECTIONS)

        # Categories panel.
        while self._cat_layout.count():
            item = self._cat_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
        cats = list(getattr(filters, "categories", []) or [])
        if not cats:
            empty = QLabel(self.tr("No categories"))
            empty.setObjectName("FilterEmpty")
            self._cat_layout.addWidget(empty)
            return
        from gui_qt.tri_state_checkbox import TriStateCheckBox
        self._cat_boxes = []
        for cid, cname, _slug in cats:
            cb = TriStateCheckBox(cname)
            cb.setToolTip(self.tr("Click once to include, twice to exclude."))
            cb._cat_id = cid        # the API filters by id, never by slug
            cb.stateChanged.connect(self._on_category_changed)
            self._cat_layout.addWidget(cb)
            self._cat_boxes.append(cb)

    def _on_category_changed(self, _state=None):
        inc, exc = [], []
        for cb in getattr(self, "_cat_boxes", []):
            st = cb.state()
            if st == 1:
                inc.append(cb._cat_id)
            elif st == 2:
                exc.append(cb._cat_id)
        self._included_cats, self._excluded_cats = inc, exc
        self._page = 0
        self._reload()

    def _toggle_categories(self, on: bool):
        if not on:
            sizes = self._body_split.sizes()
            if sizes and sizes[0] > 0:
                self._cat_width = sizes[0]
        self._cat_panel.setVisible(on)
        if on:
            total = sum(self._body_split.sizes()) or 1280
            self._body_split.setSizes(
                [self._cat_width, max(200, total - self._cat_width)])

    # -- fetching -----------------------------------------------------------
    def _reload(self):
        if not self._community:
            self._status.setText(self.tr("No Thunderstore community."))
            return
        self._fetch_token += 1
        token = self._fetch_token
        self._set_loading(True)
        # Snapshot every piece of state BEFORE the thread starts - the worker
        # must never read self.* (it can change mid-flight).
        community = self._community
        page = self._page
        query = self._query
        ordering = self._ordering
        inc = list(self._included_cats)
        exc = list(self._excluded_cats)
        section = self._section
        deprecated = self._show_deprecated
        nsfw = self._show_nsfw

        def _worker():
            entries, status, count = [], "", 0
            try:
                rows, count = fetch_listing(
                    community, page=page, query=query, ordering=ordering,
                    included_categories=inc, excluded_categories=exc,
                    section=section, deprecated=deprecated, nsfw=nsfw)
                entries = [_adapt(r) for r in rows]
                if not entries:
                    status = (f"No results for '{query}'." if query
                              else "No mods found.")
            except Exception as exc_:
                self._log(f"[thunderstore] browse failed: {exc_}")
                status = f"Error: {exc_}"
            safe_emit(self._results_ready, entries, status, token, count)

        threading.Thread(target=_worker, daemon=True,
                         name="ts-browse").start()

    def _on_results(self, entries, status, token, count):
        if token != self._fetch_token:
            return              # a newer fetch superseded this one
        self._set_loading(False)
        self._entries = list(entries or [])
        self._total = int(count or 0)
        self._rebuild_cards()
        pages = total_pages(self._total)
        if status:
            self._status.setText(status)
        else:
            self._status.setText(
                self.tr("{0} mod(s)").format(self._total))
        self._page_edit.setText(str(self._page + 1))
        self._page_total.setText(self.tr("/ {0}").format(pages))
        self._scroll.verticalScrollBar().setValue(0)
        self._update_page_buttons()

    def _set_loading(self, on: bool):
        for w in (self._prev_btn, self._next_btn, self._sort_sel,
                  self._section_sel, self._page_edit):
            w.setEnabled(not on)
        if on:
            self._status.setText(self.tr("Loading…"))
            self._loading_overlay.show_over()
        else:
            self._loading_overlay.hide_overlay()

    # -- cards --------------------------------------------------------------
    def _installed_packages(self) -> set:
        """Lowercased ``{namespace}-{name}`` ids already staged for this game.

        Thunderstore has no numeric ids, so install state matches on package
        identity - the same key filter_already_installed uses.
        """
        game = self._game
        if game is None:
            return set()
        try:
            if not game.is_configured():
                return set()
            from pathlib import Path

            from Thunderstore.thunderstore_update_checker import scan_installed
            staging = game.get_effective_mod_staging_path()
            if not staging or not Path(staging).is_dir():
                return set()
            # scan_installed yields (folder, meta) tuples.
            return {m.package_id.lower()
                    for _folder, m in scan_installed(Path(staging))
                    if m.package_id}
        except Exception:
            return set()

    def _rebuild_cards(self):
        for card in self._cards:
            card.setParent(None)
            card.deleteLater()
        self._cards = []
        installed = self._installed_packages()
        dl_only = self._download_only()
        for e in self._entries:
            card = NexusModCard(
                e, self._on_view, self._on_install,
                on_context=self._show_card_menu,
                is_installed=e.package_id.lower() in installed,
                download_only=dl_only)
            self._cards.append(card)
            self._thumbs.request(e.mod_id, e.picture_url or "")
        self._cols = 0
        self._relayout()

    @staticmethod
    def _download_only() -> bool:
        from Utils.ui_config import load_download_only
        try:
            return bool(load_download_only())
        except Exception:
            return False

    def refresh_installed(self):
        """Re-read staging and update every card's Install/Reinstall state."""
        installed = self._installed_packages()
        for card in self._cards:
            card.set_installed(card.entry.package_id.lower() in installed)

    def refresh_install_labels(self):
        """Re-read 'Download only' and relabel the card buttons."""
        flag = self._download_only()
        for card in self._cards:
            card.set_download_only(flag)

    def _cols_for_width(self) -> int:
        vp = self._scroll.viewport().width()
        slot = CARD_W + self._grid.spacing()
        return max(1, (vp - 32) // slot)

    def _relayout(self):
        cols = self._cols_for_width()
        while self._grid.count():
            self._grid.takeAt(0)
        for i, card in enumerate(self._cards):
            self._grid.addWidget(card, i // cols, 1 + (i % cols),
                                 Qt.AlignTop | Qt.AlignHCenter)
            card.show()
        for c in range(self._grid.columnCount()):
            self._grid.setColumnStretch(c, 0)
        self._grid.setColumnStretch(0, 1)
        self._grid.setColumnStretch(cols + 1, 1)
        self._cols = cols

    def _on_thumb(self, mod_id, pm):
        for card in self._cards:
            if card.entry.mod_id == mod_id:
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
    def _on_view(self, entry):
        from Utils.xdg import open_url
        # Only the community-scoped form resolves; the bare /package/{ns}/{name}/
        # URL the API reports as package_url 404s in a browser.
        open_url(package_url(entry.community or self._community,
                             entry.namespace, entry.package_name),
                 log_fn=self._log)

    def _on_install(self, entry):
        """Resolve the newest version, then hand off to the ror2mm pipeline."""
        community = self._community
        self._log(f"[thunderstore] resolving latest version of "
                  f"{entry.package_id}…")

        def _work():
            return entry, fetch_latest_version(entry.namespace,
                                               entry.package_name)

        run_in_worker(_work, self._version_ready, name="ts-latest",
                      unpack=True, error_result=(entry, ""))
        self._pending_community = community

    def _on_version_ready(self, entry, version):
        if not version:
            self._log(f"[thunderstore] could not resolve a version for "
                      f"{entry.package_id} - install skipped")
            return
        # A game switch may have retargeted this view while the lookup ran;
        # installing then would put a package into the wrong game's profile.
        if getattr(self, "_pending_community", self._community) != self._community:
            self._log("[thunderstore] game changed during lookup - "
                      "install cancelled")
            return
        self._install_fn(entry.namespace, entry.package_name, version)

    def _show_card_menu(self, entry, global_pos):
        from PySide6.QtWidgets import QMenu
        menu = QMenu(self)
        menu.addAction(self.tr("Open on Thunderstore"),
                       lambda: self._on_view(entry))
        menu.addAction(self.tr("Mods by {0}").format(entry.namespace),
                       lambda: self._search_author(entry.namespace))
        menu.exec(global_pos)

    def _search_author(self, namespace: str):
        self._search.setText(namespace)
        self._do_search_now()

    # -- search / paging ----------------------------------------------------
    def _on_search_text(self, _text: str):
        if self._search_timer is None:
            self._search_timer = QTimer(self)
            self._search_timer.setSingleShot(True)
            self._search_timer.setInterval(450)
            self._search_timer.timeout.connect(self._do_search_now)
        self._search_timer.start()

    def _do_search_now(self):
        q = self._search.text().strip()
        if q == self._query:
            return
        self._query = q
        self._page = 0
        self._reload()

    def _clear_search(self):
        self._search.clear()
        if self._query:
            self._query = ""
            self._page = 0
            self._reload()

    def _on_sort(self, label: str):
        for lbl, value in ORDERINGS:
            if lbl == label:
                if value != self._ordering:
                    self._ordering = value
                    self._page = 0
                    self._reload()
                return

    def _on_section(self, label: str):
        uuid = ""
        if label != _ALL_SECTIONS:
            for s in self._sections:
                if s[1] == label:
                    uuid = s[0]
                    break
        if uuid != self._section:
            self._section = uuid
            self._page = 0
            self._reload()

    def _on_deprecated(self, on: bool):
        self._show_deprecated = bool(on)
        self._save_flag("show_deprecated", on)
        self._page = 0
        self._reload()

    def _on_nsfw(self, on: bool):
        self._show_nsfw = bool(on)
        self._save_flag("show_nsfw", on)
        self._page = 0
        self._reload()

    def _update_page_buttons(self):
        pages = total_pages(self._total)
        self._prev_btn.setEnabled(self._page > 0)
        # The exact count gives a real last page - no "did this page fill?"
        # guessing like the Nexus browser has to do.
        self._next_btn.setEnabled(self._page + 1 < pages)

    def _prev_page(self):
        if self._page > 0:
            self._page -= 1
            self._scroll.verticalScrollBar().setValue(0)
            self._reload()

    def _next_page(self):
        if self._page + 1 < total_pages(self._total):
            self._page += 1
            self._scroll.verticalScrollBar().setValue(0)
            self._reload()

    def _jump_to_page(self):
        try:
            want = int(self._page_edit.text().strip()) - 1
        except ValueError:
            self._page_edit.setText(str(self._page + 1))
            return
        want = max(0, min(want, total_pages(self._total) - 1))
        if want != self._page:
            self._page = want
            self._scroll.verticalScrollBar().setValue(0)
            self._reload()

    # -- app hooks ----------------------------------------------------------
    def set_game(self, game, community):
        """Retarget the browser after the application changes games."""
        self._game = game
        community = (community or "").strip()
        if community == self._community:
            self.refresh_installed()
            return
        self._community = community
        self._page = 0
        self._query = ""
        self._search.blockSignals(True)
        self._search.clear()
        self._search.blockSignals(False)
        self._section = ""
        self._included_cats = []
        self._excluded_cats = []
        self._filters_loaded = False
        # Replace the thumbnail loader: `loaded` only carries the synthesised
        # id, so an in-flight image from the old community could otherwise land
        # on a same-id card in the new one.
        try:
            self._thumbs.loaded.disconnect(self._on_thumb)
        except (RuntimeError, TypeError):
            pass
        self._thumbs.deleteLater()
        self._thumbs = ThumbnailLoader(self)
        self._thumbs.loaded.connect(self._on_thumb)
        self._load_filters()
        self._reload()
