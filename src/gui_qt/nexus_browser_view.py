"""Nexus Mods browser - a full detachable tab.

Qt port of the Tk overlay (gui/nexus_browser_overlay.py + browse/trending/
tracked/endorsed_mods_panel.py + mod_card.py). Layout:

  ┌───────────────────────────────────────────────────────────┐
  │ Browse Tracked Endorsed Trending   [Sort▾][Time▾] ☐Adult …│  toolbar (blue)
  ├──────────┬────────────────────────────────────────────────┤
  │ Categories│                card grid (pink)                │
  │ (green)   │                                                │
  ├──────────┴────────────────────────────────────────────────┤
  │ [search……] Search ✕   ◂ Prev  Next ▸  page [ ] / N   status│  footer (yellow)
  └───────────────────────────────────────────────────────────┘

All Nexus data + install logic comes from the toolkit-neutral Nexus/ layer; this
file is pure Qt UI + threading. Fetches run on worker threads and marshal results
back via signals. The active game supplies the primary and compatible Nexus
domains; the selector chooses which one this browser session uses.
"""

from __future__ import annotations

import threading

from PySide6.QtCore import Qt, QTimer, Signal, QEvent, QDate, QStringListModel
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QLabel, QLineEdit,
    QScrollArea, QFrame, QCheckBox, QToolButton, QMenu,
    QSplitter, QCompleter, QComboBox,
)

from gui_qt.theme_qt import active_palette, _c
from gui_qt.flow_layout import FlowLayout
from gui_qt.safe_emit import safe_emit
from gui_qt.worker import run_in_worker
from gui_qt.selector_button import SelectorButton
from gui_qt.nexus_mod_card import NexusModCard, ThumbnailLoader, CARD_W

# label → API value (verbatim from Tk browse_mods_panel.SORT_KEYS / TIME_RANGES)
SORT_KEYS = [
    ("Downloads", "downloads"),
    ("Date Published", "createdAt"),
    ("Endorsements", "endorsements"),
    ("Last Updated", "updatedAt"),
]
TIME_RANGES = [
    ("All time", None),
    ("24 hours", 1),
    ("7 days", 7),
    ("14 days", 14),
    ("28 days", 28),
    ("3 months", 90),
    ("6 months", 180),
    ("Year", 365),
]
SECTIONS = ["Browse", "Tracked", "Endorsed", "Trending"]
# Common mod-translation languages on Nexus. Not exhaustive - the API doesn't
# expose a list to query, so this is a curated set matching what shows up in
# practice (mirrors the site's own Language Support filter roughly).
LANGUAGES = [
    "English", "French", "German", "Spanish", "Italian", "Polish",
    "Portuguese", "Russian", "Chinese", "Japanese", "Korean", "Turkish",
    "Dutch", "Czech", "Hungarian", "Swedish", "Ukrainian", "Arabic",
    "Vietnamese", "Thai", "Indonesian",
]
# File Size / Downloads / Endorsements min/max dropdown presets - lifted
# straight from the site's own filter menus (values confirmed live), not
# invented breakpoints, so a user picking "50MB" here gets the same result
# they'd get there.
FILE_SIZE_PRESETS = [
    ("1KB", 1), ("100KB", 100), ("500KB", 500), ("1MB", 1024),
    ("50MB", 51_200), ("100MB", 102_400), ("500MB", 512_000),
    ("1GB", 1_048_576), ("5GB", 5_242_880), ("10GB", 10_485_760),
]
DOWNLOADS_PRESETS = [
    ("50", 50), ("100", 100), ("1,000", 1_000), ("5,000", 5_000),
    ("10,000", 10_000), ("50,000", 50_000), ("100,000", 100_000),
    ("500,000", 500_000), ("1,000,000", 1_000_000), ("5,000,000", 5_000_000),
]
ENDORSEMENTS_PRESETS = [
    ("10", 10), ("50", 50), ("100", 100), ("1,000", 1_000), ("5,000", 5_000),
    ("10,000", 10_000), ("50,000", 50_000), ("100,000", 100_000),
    ("300,000", 300_000),
]
# Default "shown per page"; overridden by the footer dropdown (persisted).
PAGE_SIZE_BROWSE = 30
# User-selectable "shown per page" counts (footer dropdown).
PAGE_SIZE_CHOICES = [20, 30, 40, 50]


def _category_key(value: str) -> str:
    """Normalise a category/game name for comparison with a Nexus domain."""
    return "".join(char for char in (value or "").casefold()
                   if char.isalnum())


def _visible_categories(categories, game_name: str, domain: str):
    """Exclude Nexus's synthetic game-name category from the filter list."""
    game_keys = {_category_key(game_name), _category_key(domain)} - {""}
    return [
        category for category in categories
        if category.category_id != 0
        and _category_key(category.name) not in game_keys
    ]


class NexusBrowserView(QWidget):
    """Required: *api* (authed NexusAPI), *domain* (game.nexus_game_domain),
    *game*. Optional: *install_fn(list[str])* (defaults to a no-op), *log_fn*."""

    _results_ready = Signal(object, str, object)   # (entries, status, token)
    _cats_ready = Signal(object, str)               # (list[NexusCategory], domain)
    _tags_ready = Signal(object, str)               # (list[NexusTag], domain)
    _premium_checked = Signal(object, object)       # (entry, is_premium|None)
    _files_ready = Signal(object, object)           # (entry, list[NexusModFile])
    _manual_files_ready = Signal(object, object)    # (entry, list[NexusModFile]|None)
    _manual_watch_ended = Signal(int)               # (mod_id) - found or timed out
    _download_done = Signal(object, object, object)      # (archive_path|None, meta|None, dl_key)
    _download_progress = Signal(object, object, "qlonglong", "qlonglong")  # (dl_key, name, downloaded, total bytes; 64-bit: >2GB)

    def __init__(self, api, domain, game, install_fn=None, log_fn=None,
                 progress_fn=None, parent=None):
        super().__init__(parent)
        self._api = api
        domains = tuple(getattr(game, "nexus_game_domains", ()) or ())
        self._domains = domains or tuple(
            d for d in ((domain or "").strip().lower(),) if d)
        self._domain = self._domains[0] if self._domains else ""
        self._game = game
        self._install_fn = install_fn or (lambda paths, metas=None: None)
        self._log = log_fn or (lambda m: None)
        # progress_fn(key, name, downloaded, total) reports one download's
        # bytes to the host (which combines concurrent downloads into a single
        # progress card); total<0 means "this download finished". Defaults to
        # a no-op.
        self._progress_fn = progress_fn or (lambda *args: None)
        self._dl_seq = 0                # unique progress-card key per download

        # state
        self._section = "Browse"
        self._page = 0
        self._sort_key = "downloads"
        self._time_days = None
        self._custom_time_label: str | None = None   # e.g. "Since 2026-03-01"
        self._custom_time_date: QDate | None = None   # the picked calendar date
        self._query = ""
        self._search_mode = "Name"      # "Name" | "Author" (Browse search bar)
        # When an author browse is launched from a card's right-click, we search
        # by the uploader's stable account id (reliable) rather than the typed
        # name. Non-zero id here → the Author-mode worker uses the id path.
        self._uploader_id = 0
        self._selected_categories: list[str] = []
        # None = no adult filter (site's "neither box checked"), True = only
        # adult, False = hidden. Persisted default mirrors the old toolbar
        # toggle: hidden unless the user previously chose to see everything.
        self._adult: bool | None = False if not self._load_show_adult() else None
        self._page_size_choice = self._load_page_size()
        self._entries = []
        self._cards: list[NexusModCard] = []
        self._cols = 0
        self._fetch_token = 0           # guards against stale async results
        self._cats_loaded = False

        # advanced search state (see Nexus.nexus_api.NexusSearchFilters)
        self._tags_loaded = False
        self._game_tags: list = []          # list[NexusTag] for the active domain
        self._tag_includes: list[str] = []
        self._tag_excludes: list[str] = []
        self._languages: list[str] = []
        self._hide_translations = False
        self._min_size_kb: int | None = None
        self._max_size_kb: int | None = None
        self._min_downloads: int | None = None
        self._max_downloads: int | None = None
        self._min_endorsements: int | None = None
        self._max_endorsements: int | None = None
        self._supports_vortex: bool | None = None
        self._has_updated = False
        self._title_contains = ""
        self._description_contains = ""
        self._author_contains = ""
        self._uploader_contains = ""
        # Coalesces a burst of advanced-filter edits into one refetch.
        self._advanced_timer = QTimer(self)
        self._advanced_timer.setSingleShot(True)
        self._advanced_timer.setInterval(600)
        self._advanced_timer.timeout.connect(self._apply_advanced_filters)

        self._thumbs = ThumbnailLoader(self)
        self._thumbs.loaded.connect(self._on_thumb)
        self._results_ready.connect(self._on_results)
        self._cats_ready.connect(self._on_cats)
        self._tags_ready.connect(self._on_tags_ready)
        self._premium_checked.connect(self._on_premium_checked)
        self._files_ready.connect(self._on_files_ready)
        self._manual_files_ready.connect(self._on_manual_files_ready)
        # Bound method (NOT a lambda): slot connections to a QObject method are
        # dropped by Qt when the receiver is destroyed, so a watcher thread
        # finishing mid-teardown can't fire into a dead wrapper.
        self._manual_watch_ended.connect(self._on_manual_watch_ended)
        self._download_done.connect(self._on_download_done)
        self._download_progress.connect(self._on_download_progress)
        self._installing = False        # serialise the prep phase (premium
        #                                 check → file chooser) of one install;
        #                                 released once the download starts
        # mod_id → (ManualDownloadWatcher, dl_key): non-premium installs
        # waiting for a browser ("Slow") download to land in the download
        # folders. The destroyed hook must not touch self (C++ side is gone
        # by then), so it captures the dict + progress_fn directly.
        self._manual_watchers: dict = {}

        def _stop_watchers(*_, w=self._manual_watchers, pf=self._progress_fn):
            for watcher, key in list(w.values()):
                watcher.stop()
                try:
                    pf(key, "", 0, -1)
                except Exception:
                    pass
            w.clear()
        self.destroyed.connect(_stop_watchers)

        self._build()
        self._update_section_buttons()
        self._update_browse_controls_visibility()
        self._load_categories()
        self._load_tags()
        self._reload()

    # -- construction -------------------------------------------------------
    @staticmethod
    def _filter_qss(p) -> str:
        """Match the modlist filter side-panel styling (same #Filter* QSS) so
        the categories panel's font, header and backgrounds look identical."""
        c = lambda k: _c(p, k)
        return f"""
        #FilterPanel {{ background: {c('BG_PANEL')}; }}
        #FilterHeader {{ background: {c('BG_HEADER')}; }}
        #FilterTitle {{ font-weight: bold; font-size: 14px; color: {c('TEXT_MAIN')}; }}
        #FilterRule {{ background: {c('BORDER')}; }}
        #FilterBody {{ background: {c('BG_PANEL')}; }}
        #FilterEmpty {{ color: {c('TEXT_DIM')}; font-style: italic; }}
        QScrollArea {{ background: {c('BG_PANEL')}; border: none; }}
        #CollapsibleSection {{
            border: 1px solid {c('BORDER')};
            border-radius: 6px;
            background: {c('BG_PANEL')};
        }}
        QToolButton#SectionToggle {{
            background: transparent; border: none;
            font-weight: bold; color: {c('TEXT_MAIN')};
            padding: 2px 0;
        }}
        QToolButton#SectionToggle:hover,
        QToolButton#SectionToggle:pressed,
        QToolButton#SectionToggle:checked {{
            background: transparent; color: {c('TEXT_MAIN')};
        }}
        """

    @staticmethod
    def _load_show_adult() -> bool:
        try:
            from Utils.ui_config import load_nexus_show_adult
            return bool(load_nexus_show_adult())
        except Exception:
            return False

    @staticmethod
    def _load_page_size() -> int:
        try:
            from Utils.ui_config import load_nexus_page_size
            return int(load_nexus_page_size(PAGE_SIZE_BROWSE))
        except Exception:
            return PAGE_SIZE_BROWSE

    def _add_filter_section(self, host_layout: QVBoxLayout, title: str,
                            *, expanded: bool = False):
        """Add an openable/closable section, returning its body layout."""
        from gui_qt.collapsible_section import CollapsibleSection
        sec = CollapsibleSection(title)
        if expanded:
            sec.expand()
        body_layout = QVBoxLayout(sec.body)
        body_layout.setContentsMargins(0, 0, 0, 0)
        body_layout.setSpacing(4)
        host_layout.addWidget(sec)
        return body_layout

    @staticmethod
    def _range_combo(presets: list[tuple[str, int]], no_label: str) -> "QComboBox":
        """A min/max dropdown: a "no limit" entry plus the site's presets."""
        # "No min"/"No max" carries data=None; each preset carries the raw
        # filter value the site itself sends.
        cb = QComboBox()
        cb.addItem(no_label, None)
        for label, value in presets:
            cb.addItem(label, value)
        return cb

    def _build_advanced_search_section(self, host_layout: QVBoxLayout) -> None:
        """Build the rest of the site's filter panel below Category."""
        # Tags, Search Parameters, Language Support, Content Options, File
        # Size, Downloads, Endorsements - each its own openable/closable
        # section like the site's. Built once (unlike Category, this doesn't
        # get rebuilt per game); _reload() reads the live widget state each
        # fetch.

        def _tag_edit(placeholder: str) -> QLineEdit:
            e = QLineEdit()
            e.setPlaceholderText(placeholder)
            completer = QCompleter([], self)
            completer.setCaseSensitivity(Qt.CaseInsensitive)
            completer.setFilterMode(Qt.MatchContains)
            e.setCompleter(completer)
            return e

        # -- Tags -------------------------------------------------------
        lay = self._add_filter_section(host_layout, self.tr("Tags"))
        lay.addWidget(QLabel(self.tr("Includes")))
        self._tag_include_edit = _tag_edit(self.tr("Type a tag, press Enter…"))
        self._tag_include_edit.returnPressed.connect(
            lambda: self._add_tag(self._tag_include_edit, include=True))
        lay.addWidget(self._tag_include_edit)
        self._tag_include_row = QWidget()
        self._tag_include_flow = FlowLayout(self._tag_include_row, spacing=4)
        lay.addWidget(self._tag_include_row)

        lay.addWidget(QLabel(self.tr("Excludes")))
        self._tag_exclude_edit = _tag_edit(self.tr("Type a tag, press Enter…"))
        self._tag_exclude_edit.returnPressed.connect(
            lambda: self._add_tag(self._tag_exclude_edit, include=False))
        lay.addWidget(self._tag_exclude_edit)
        self._tag_exclude_row = QWidget()
        self._tag_exclude_flow = FlowLayout(self._tag_exclude_row, spacing=4)
        lay.addWidget(self._tag_exclude_row)

        # -- Search Parameters -------------------------------------------
        # title_contains overlaps with the browse bar's search (ANDs with it
        # rather than replacing it - see NexusSearchFilters). Debounced via
        # _on_advanced_changed rather than sitting behind a separate Apply
        # button like the site's panel does.
        lay = self._add_filter_section(host_layout, self.tr("Search Parameters"))
        self._title_contains_edit = QLineEdit()
        self._title_contains_edit.setPlaceholderText(self.tr("Title contains"))
        lay.addWidget(self._title_contains_edit)
        self._description_contains_edit = QLineEdit()
        self._description_contains_edit.setPlaceholderText(self.tr("Description contains"))
        lay.addWidget(self._description_contains_edit)
        self._author_contains_edit = QLineEdit()
        self._author_contains_edit.setPlaceholderText(self.tr("Author contains"))
        lay.addWidget(self._author_contains_edit)
        self._uploader_contains_edit = QLineEdit()
        self._uploader_contains_edit.setPlaceholderText(self.tr("Uploader contains"))
        lay.addWidget(self._uploader_contains_edit)
        for edit in (self._title_contains_edit, self._description_contains_edit,
                     self._author_contains_edit, self._uploader_contains_edit):
            edit.textChanged.connect(self._on_advanced_changed)

        # -- Language Support ---------------------------------------------
        # Multi-select (OR'd - "any of these languages"), same TriStateCheckBox
        # the category list uses, matching the site's checkbox list rather
        # than a single-choice dropdown.
        from gui_qt.tri_state_checkbox import TriStateCheckBox
        lay = self._add_filter_section(host_layout, self.tr("Language Support"))
        self._hide_translations_cb = TriStateCheckBox(
            self.tr("Hide translations"), two_state=True)
        self._hide_translations_cb.stateChanged.connect(
            lambda _s: self._on_advanced_changed())
        lay.addWidget(self._hide_translations_cb)
        self._lang_checks: list[TriStateCheckBox] = []
        for lang in LANGUAGES:
            cb = TriStateCheckBox(lang, two_state=True)
            cb._lang_name = lang
            cb.stateChanged.connect(lambda _s: self._on_advanced_changed())
            lay.addWidget(cb)
            self._lang_checks.append(cb)

        # -- Content Options ------------------------------------------------
        # Hide/Show-only adult are mutually exclusive (site behaves the same
        # way, even though it renders them as independent checkboxes).
        lay = self._add_filter_section(host_layout, self.tr("Content Options"))
        self._hide_adult_cb = TriStateCheckBox(self.tr("Hide adult content"), two_state=True)
        self._hide_adult_cb.stateChanged.connect(lambda _s: self._on_hide_adult_toggled())
        lay.addWidget(self._hide_adult_cb)
        self._show_only_adult_cb = TriStateCheckBox(
            self.tr("Show only adult content"), two_state=True)
        self._show_only_adult_cb.stateChanged.connect(
            lambda _s: self._on_show_only_adult_toggled())
        lay.addWidget(self._show_only_adult_cb)
        self._supports_vortex_cb = TriStateCheckBox(self.tr("Supported by Vortex"), two_state=True)
        self._supports_vortex_cb.stateChanged.connect(
            lambda _s: self._on_advanced_changed())
        lay.addWidget(self._supports_vortex_cb)
        self._only_updated_cb = TriStateCheckBox(self.tr("Show only updated mods"), two_state=True)
        self._only_updated_cb.stateChanged.connect(
            lambda _s: self._on_advanced_changed())
        lay.addWidget(self._only_updated_cb)
        self._hide_adult_cb.set_state(1 if self._adult is False else 0)

        # -- File Size / Downloads / Endorsements --------------------------
        # Dropdown presets, not free-text - lifted straight from the site's
        # own min/max menus (verified against the live filter UI).
        lay = self._add_filter_section(host_layout, self.tr("File Size"))
        size_row = QHBoxLayout()
        self._min_size_combo = self._range_combo(FILE_SIZE_PRESETS, self.tr("No min"))
        self._max_size_combo = self._range_combo(FILE_SIZE_PRESETS, self.tr("No max"))
        size_row.addWidget(self._min_size_combo)
        size_row.addWidget(QLabel(self.tr("to")))
        size_row.addWidget(self._max_size_combo)
        lay.addLayout(size_row)

        lay = self._add_filter_section(host_layout, self.tr("Downloads"))
        dl_row = QHBoxLayout()
        self._min_downloads_combo = self._range_combo(DOWNLOADS_PRESETS, self.tr("No min"))
        self._max_downloads_combo = self._range_combo(DOWNLOADS_PRESETS, self.tr("No max"))
        dl_row.addWidget(self._min_downloads_combo)
        dl_row.addWidget(QLabel(self.tr("to")))
        dl_row.addWidget(self._max_downloads_combo)
        lay.addLayout(dl_row)

        lay = self._add_filter_section(host_layout, self.tr("Endorsements"))
        end_row = QHBoxLayout()
        self._min_endorsements_combo = self._range_combo(ENDORSEMENTS_PRESETS, self.tr("No min"))
        self._max_endorsements_combo = self._range_combo(ENDORSEMENTS_PRESETS, self.tr("No max"))
        end_row.addWidget(self._min_endorsements_combo)
        end_row.addWidget(QLabel(self.tr("to")))
        end_row.addWidget(self._max_endorsements_combo)
        lay.addLayout(end_row)

        for combo in (self._min_size_combo, self._max_size_combo,
                     self._min_downloads_combo, self._max_downloads_combo,
                     self._min_endorsements_combo, self._max_endorsements_combo):
            combo.currentIndexChanged.connect(lambda _i: self._on_advanced_changed())

        clear_btn = QToolButton()
        clear_btn.setText(self.tr("Clear advanced filters"))
        clear_btn.setObjectName("ActionButton")
        clear_btn.setCursor(Qt.PointingHandCursor)
        clear_btn.clicked.connect(self._clear_advanced_filters)
        host_layout.addWidget(clear_btn)

    @staticmethod
    def _enable_hfw(w: QWidget) -> None:
        """Report the bar's height via heightForWidth (the wrapped FlowLayout
        height at the actual width), not the sizeHint Qt evaluates at minimum
        width (every control on its own row) - same as app._enable_height_for_width."""
        pol = w.sizePolicy()
        pol.setHeightForWidth(True)
        w.setSizePolicy(pol)

    def _build(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        p = active_palette()

        # --- blue toolbar ---------------------------------------------------
        # FlowLayout, not QHBoxLayout: pinned into a panel this view can get
        # much narrower than one row of buttons, and the un-wrappable row's
        # minimum width would poison the panel stack's minimum (a QStackedWidget
        # takes the max over ALL its pages), jamming the body splitter for every
        # other panel tab. Wrapping keeps our minimum width to the widest single
        # control instead.
        toolbar = QWidget()
        toolbar.setObjectName("HeaderBar")
        tb = FlowLayout(toolbar, spacing=6)
        tb.setContentsMargins(10, 6, 10, 6)

        self._cat_toggle = QToolButton()
        self._cat_toggle.setText(self.tr("☰ Filters"))
        self._cat_toggle.setObjectName("ActionButton")
        self._cat_toggle.setCheckable(True)
        self._cat_toggle.setChecked(True)
        self._cat_toggle.setCursor(Qt.PointingHandCursor)
        self._cat_toggle.toggled.connect(self._toggle_categories)
        tb.addWidget(self._cat_toggle)

        self._domain_sel = SelectorButton(
            items=list(self._domains), current=self._domain,
            prefix=self.tr("Domain: "), min_width=190,
            on_select=self._on_domain_changed)
        self._domain_sel.setToolTip(self.tr(
            "Choose which compatible Nexus game to browse."))
        self._domain_sel.setVisible(len(self._domains) > 1)
        tb.addWidget(self._domain_sel)

        self._section_btns: dict[str, QToolButton] = {}
        for name in SECTIONS:
            b = QToolButton()
            b.setText(name)
            b.setObjectName("ActionButton")
            b.setCheckable(True)
            b.setCursor(Qt.PointingHandCursor)
            b.clicked.connect(lambda _=False, n=name: self._set_section(n))
            tb.addWidget(b)
            self._section_btns[name] = b

        tb.add_stretch()

        self._sort_sel = SelectorButton(
            items=[lbl for lbl, _ in SORT_KEYS], current="Downloads",
            prefix="Sort: ", min_width=150, on_select=self._on_sort_changed)
        tb.addWidget(self._sort_sel)
        self._time_sel = SelectorButton(
            items=[lbl for lbl, _ in TIME_RANGES], current="All time",
            prefix="Time: ", min_width=130, on_select=self._on_time_changed,
            actions=[(self.tr("Custom…"), self._pick_custom_time)])
        tb.addWidget(self._time_sel)

        refresh = QToolButton()
        refresh.setText(self.tr("Refresh"))
        refresh.setObjectName("ActionButton")
        refresh.setCursor(Qt.PointingHandCursor)
        refresh.clicked.connect(self._reload)
        tb.addWidget(refresh)

        self._enable_hfw(toolbar)
        outer.addWidget(toolbar)

        # --- body: categories (green) + card grid (pink) -------------------
        # A QSplitter lets the user drag the divider to resize the categories
        # panel; the toolbar's "Categories" toggle hides/shows it.
        self._body_split = QSplitter(Qt.Horizontal)
        self._body_split.setChildrenCollapsible(True)
        self._body_split.setHandleWidth(6)

        # categories panel (resizable, bounded). Default width fits 3 cards in
        # the grid at the 1280 min window width. Styled to match the modlist
        # filters side-panel (same #Filter* object names + QSS).
        self._cat_panel = QWidget()
        self._cat_panel.setObjectName("FilterPanel")
        self._cat_panel.setMinimumWidth(120)
        self._cat_panel.setMaximumWidth(460)
        cv = QVBoxLayout(self._cat_panel)
        cv.setContentsMargins(0, 0, 0, 0)
        cv.setSpacing(0)
        cat_header = QWidget()
        cat_header.setObjectName("FilterHeader")
        chl = QHBoxLayout(cat_header)
        chl.setContentsMargins(10, 6, 8, 6)
        cat_hdr = QLabel(self.tr("Filters"))
        cat_hdr.setObjectName("FilterTitle")
        chl.addWidget(cat_hdr)
        chl.addStretch(1)
        cv.addWidget(cat_header)
        cat_rule = QFrame()
        cat_rule.setObjectName("FilterRule")
        cat_rule.setFixedHeight(1)
        cv.addWidget(cat_rule)
        self._cat_scroll = QScrollArea()
        self._cat_scroll.setWidgetResizable(True)
        self._cat_scroll.setFrameShape(QFrame.NoFrame)
        # _cat_host holds every section, each an openable/closable
        # CollapsibleSection like the site's own filter panel. Category is
        # rebuilt per game (_on_cats, via _cat_layout inside its section body);
        # the rest are built once here and never rebuilt.
        self._cat_host = QWidget()
        self._cat_host.setObjectName("FilterBody")
        host_layout = QVBoxLayout(self._cat_host)
        host_layout.setContentsMargins(10, 8, 10, 12)
        host_layout.setSpacing(6)
        host_layout.setAlignment(Qt.AlignTop)

        from gui_qt.collapsible_section import CollapsibleSection
        cat_section = CollapsibleSection(self.tr("Category"))
        cat_section.expand()             # open by default, matching current behaviour
        cat_body_layout = QVBoxLayout(cat_section.body)
        cat_body_layout.setContentsMargins(0, 0, 0, 0)
        cat_list_host = QWidget()
        self._cat_layout = QVBoxLayout(cat_list_host)
        self._cat_layout.setContentsMargins(0, 0, 0, 0)
        self._cat_layout.setSpacing(3)
        self._cat_layout.setAlignment(Qt.AlignTop)
        cat_body_layout.addWidget(cat_list_host)
        self._cat_checks: list[QCheckBox] = []
        self._cat_status = QLabel(self.tr("Loading…"))
        self._cat_status.setObjectName("FilterEmpty")
        self._cat_layout.addWidget(self._cat_status)
        host_layout.addWidget(cat_section)

        self._build_advanced_search_section(host_layout)

        self._cat_scroll.setWidget(self._cat_host)
        cv.addWidget(self._cat_scroll, 1)
        self._cat_panel.setStyleSheet(self._filter_qss(p))
        self._body_split.addWidget(self._cat_panel)

        # card grid
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
        from gui_qt.loading_overlay import LoadingOverlay
        self._loading_overlay = LoadingOverlay(self._scroll)
        self._body_split.addWidget(self._scroll)

        self._body_split.setStretchFactor(0, 0)
        self._body_split.setStretchFactor(1, 1)
        # ~260px categories: the grid then needs >=968px for 3 columns
        # ((3*CARD_W + 2*spacing + 32 margins)); at the 1280 min window the grid
        # gets ~1000px, so 3 cards fit. 300 was just over the threshold → 2.
        self._body_split.setSizes([260, 1020])
        outer.addWidget(self._body_split, 1)

        # --- yellow footer --------------------------------------------------
        # FlowLayout for the same reason as the toolbar: wrap when narrow
        # instead of imposing a full-row minimum width on the host panel.
        footer = QWidget()
        footer.setObjectName("HeaderBar")
        ft = FlowLayout(footer, spacing=6)
        ft.setContentsMargins(10, 6, 10, 6)

        # Search-field mode: the labels are translated for display, but we map
        # each back to a canonical English key ("Name"/"Author") so the worker's
        # comparisons don't break under translation.
        self._mode_label_name = self.tr("Name")
        self._mode_label_author = self.tr("Author")
        self._mode_sel = SelectorButton(
            items=[self._mode_label_name, self._mode_label_author],
            current=self._mode_label_name, min_width=110,
            on_select=self._on_search_mode_changed)
        ft.addWidget(self._mode_sel)

        self._search = QLineEdit()
        self._search.setPlaceholderText(self.tr("Search mods…"))
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

        cbtn = QToolButton()
        cbtn.setText(self.tr("Clear"))
        cbtn.setObjectName("ActionButton")
        cbtn.setCursor(Qt.PointingHandCursor)
        cbtn.clicked.connect(self._clear_search)
        ft.addWidget(cbtn)

        ft.add_stretch()

        self._perpage_sel = SelectorButton(
            items=[str(n) for n in PAGE_SIZE_CHOICES],
            current=str(self._page_size_choice),
            prefix="Show: ", min_width=110,
            on_select=self._on_page_size_changed)
        ft.addWidget(self._perpage_sel)

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

        self._enable_hfw(footer)
        outer.addWidget(footer)

    # -- section / control state -------------------------------------------
    def _set_section(self, name: str):
        if name == self._section:
            self._section_btns[name].setChecked(True)
            return
        self._section = name
        self._page = 0
        self._query = ""
        self._search.blockSignals(True)
        self._search.clear()
        self._search.blockSignals(False)
        self._reset_search_mode()
        self._update_section_buttons()
        self._update_browse_controls_visibility()
        self._reload()

    def _update_section_buttons(self):
        for n, b in self._section_btns.items():
            b.setChecked(n == self._section)

    def _update_browse_controls_visibility(self):
        browse = self._section == "Browse"
        paged = self._section in ("Browse", "Trending")
        self._sort_sel.setVisible(browse)
        self._time_sel.setVisible(browse)
        self._mode_sel.setVisible(browse)
        for w in (self._prev_btn, self._next_btn, self._page_edit,
                  self._perpage_sel):
            w.setVisible(paged)

    def _toggle_categories(self, on: bool):
        if on:
            self._cat_panel.setVisible(True)
            self._body_split.setSizes(
                [getattr(self, "_cat_width", 260), max(1, self._scroll.width())])
        else:
            # Remember the current width so we can restore it, then hide.
            sizes = self._body_split.sizes()
            if sizes and sizes[0] > 0:
                self._cat_width = sizes[0]
            self._cat_panel.setVisible(False)

    # -- categories ---------------------------------------------------------
    def _load_categories(self):
        if self._cats_loaded or not self._domain:
            return

        domain = self._domain
        run_in_worker(
            lambda: (self._api.get_game_categories(domain), domain),
            self._cats_ready, name="nexus-categories", unpack=True,
            error_result=([], domain))

    def _on_cats(self, cats, domain: str):
        if domain != self._domain:
            return                       # stale result from the previous domain
        self._cats_loaded = True
        cats = _visible_categories(
            cats, getattr(self._game, "name", "") or "", domain)
        # clear existing (checks + any indent-wrapper rows + the status label)
        while self._cat_layout.count():
            it = self._cat_layout.takeAt(0)
            w = it.widget()
            if w is not None:
                w.deleteLater()
        self._cat_checks.clear()
        if not cats:
            self._cat_status = QLabel(self.tr("No categories"))
            self._cat_status.setStyleSheet(
                f"color:{_c(active_palette(),'TEXT_DIM')}; padding:2px;")
            self._cat_layout.addWidget(self._cat_status)
            return
        # parents first, then their children indented (Tk hierarchy).
        by_parent: dict = {}
        for c in cats:
            by_parent.setdefault(c.parent_category, []).append(c)
        tops = sorted(by_parent.get(None, []), key=lambda c: c.name.lower())
        for top in tops:
            self._add_cat_check(top.name, indent=0)
            for child in sorted(by_parent.get(top.category_id, []),
                                key=lambda c: c.name.lower()):
                self._add_cat_check(child.name, indent=1)

    def _add_cat_check(self, name: str, indent: int):
        # Use the SAME widget as the modlist filter panel (TriStateCheckBox) so
        # the rows look identical - two_state so it's plain on/off (no exclude).
        from gui_qt.tri_state_checkbox import TriStateCheckBox
        cb = TriStateCheckBox(name, two_state=True)
        cb.setToolTip(name)              # long names that clip still readable
        cb.stateChanged.connect(lambda _s: self._on_category_toggled())
        cb._cat_name = name
        if indent:
            row = QWidget()
            rl = QHBoxLayout(row)
            rl.setContentsMargins(indent * 14, 0, 0, 0)
            rl.setSpacing(0)
            rl.addWidget(cb)
            self._cat_layout.addWidget(row)
        else:
            self._cat_layout.addWidget(cb)
        self._cat_checks.append(cb)

    def _on_category_toggled(self):
        self._selected_categories = [
            cb._cat_name for cb in self._cat_checks if cb.state()]
        self._page = 0
        self._reload()

    # -- advanced search: tags / language / size / downloads / endorsements -
    def _load_tags(self):
        if self._tags_loaded or not self._domain:
            return
        domain = self._domain
        run_in_worker(
            lambda: (self._api.get_game_tags(domain), domain),
            self._tags_ready, name="nexus-tags", unpack=True,
            error_result=([], domain))

    def _on_tags_ready(self, tags, domain: str):
        if domain != self._domain:
            return                       # stale result from the previous domain
        self._tags_loaded = True
        self._game_tags = list(tags or [])
        names = sorted({t.name for t in self._game_tags}, key=str.lower)
        for edit in (self._tag_include_edit, self._tag_exclude_edit):
            completer = edit.completer()
            if completer is not None:
                completer.setModel(QStringListModel(names, completer))

    def _add_tag(self, edit: QLineEdit, include: bool):
        typed = edit.text().strip()
        edit.clear()
        if not typed:
            return
        # Match the fetched tag list case-insensitively so the stored name is
        # exactly what the API expects; fall back to the typed text verbatim
        # if the tag list hasn't loaded yet or has no match.
        name = next((t.name for t in self._game_tags
                    if t.name.lower() == typed.lower()), typed)
        target = self._tag_includes if include else self._tag_excludes
        other = self._tag_excludes if include else self._tag_includes
        if name in other:
            other.remove(name)           # a tag can't be both included and excluded
        if name not in target:
            target.append(name)
        self._rebuild_tag_chips()
        self._page = 0
        self._reload()

    def _remove_tag(self, name: str, include: bool):
        target = self._tag_includes if include else self._tag_excludes
        if name in target:
            target.remove(name)
        self._rebuild_tag_chips()
        self._page = 0
        self._reload()

    def _rebuild_tag_chips(self):
        for flow, names, include in (
            (self._tag_include_flow, self._tag_includes, True),
            (self._tag_exclude_flow, self._tag_excludes, False),
        ):
            while flow.count():
                it = flow.takeAt(0)
                w = it.widget()
                if w is not None:
                    w.deleteLater()
            for name in names:
                chip = QToolButton()
                chip.setText(f"{name}  ✕")
                chip.setObjectName("ActionButton")
                chip.setCursor(Qt.PointingHandCursor)
                chip.setToolTip(self.tr("Remove"))
                chip.clicked.connect(
                    lambda _=False, n=name, inc=include: self._remove_tag(n, inc))
                flow.addWidget(chip)

    def _on_advanced_changed(self, _ignored=None):
        """Queue an advanced-filter refetch (debounced)."""
        # Every advanced control funnels through here so a burst of edits -
        # typing, ticking five languages, picking a min then a max - collapses
        # into a single reload instead of one round trip per keystroke/click.
        # Doubles as the fix for the read-your-own-writes race: the widget
        # state is scraped once, in _apply_advanced_filters, after the pause.
        self._advanced_timer.start()

    def _apply_advanced_filters(self):
        """Scrape every advanced widget into filter state and refetch."""
        self._languages = [cb._lang_name for cb in self._lang_checks if cb.state()]
        self._hide_translations = bool(self._hide_translations_cb.state())
        self._supports_vortex = True if self._supports_vortex_cb.state() else None
        self._has_updated = bool(self._only_updated_cb.state())
        self._min_size_kb = self._min_size_combo.currentData()
        self._max_size_kb = self._max_size_combo.currentData()
        self._min_downloads = self._min_downloads_combo.currentData()
        self._max_downloads = self._max_downloads_combo.currentData()
        self._min_endorsements = self._min_endorsements_combo.currentData()
        self._max_endorsements = self._max_endorsements_combo.currentData()
        self._title_contains = self._title_contains_edit.text().strip()
        self._description_contains = self._description_contains_edit.text().strip()
        self._author_contains = self._author_contains_edit.text().strip()
        self._uploader_contains = self._uploader_contains_edit.text().strip()
        self._page = 0
        self._reload()

    def _reset_advanced_filter_state(self, *, keep_adult: bool = True):
        """Clear the advanced filters without reloading."""
        # Used by the "Clear" button (which reloads after) and by domain
        # retargeting (which reloads once, later, after everything else
        # resets). keep_adult: the adult choice is a user preference, not a
        # game-scoped filter - a game switch must NOT silently un-hide adult
        # content, so only the explicit "Clear" button passes keep_adult=False.
        self._advanced_timer.stop()      # drop any refetch queued pre-reset
        self._tag_includes.clear()
        self._tag_excludes.clear()
        self._rebuild_tag_chips()
        self._languages = []
        for cb in self._lang_checks:
            cb.set_state(0)              # emit=False: no reload storm here
        self._hide_translations = False
        self._hide_translations_cb.set_state(0)
        if not keep_adult:
            self._adult = None
            self._hide_adult_cb.set_state(0)
            self._show_only_adult_cb.set_state(0)
            self._save_adult_pref()
        self._supports_vortex = None
        self._supports_vortex_cb.set_state(0)
        self._has_updated = False
        self._only_updated_cb.set_state(0)
        for combo in (self._min_size_combo, self._max_size_combo,
                     self._min_downloads_combo, self._max_downloads_combo,
                     self._min_endorsements_combo, self._max_endorsements_combo):
            combo.blockSignals(True)
            combo.setCurrentIndex(0)
            combo.blockSignals(False)
        self._min_size_kb = self._max_size_kb = None
        self._min_downloads = self._max_downloads = None
        self._min_endorsements = self._max_endorsements = None
        for edit in (self._title_contains_edit, self._description_contains_edit,
                     self._author_contains_edit, self._uploader_contains_edit):
            edit.blockSignals(True)
            edit.clear()
            edit.blockSignals(False)
        self._title_contains = self._description_contains = ""
        self._author_contains = self._uploader_contains = ""

    def _clear_advanced_filters(self):
        self._reset_advanced_filter_state(keep_adult=False)
        self._page = 0
        self._reload()

    def _current_search_filters(self):
        """Build a NexusSearchFilters from the live advanced-search widgets."""
        from Nexus.nexus_api import NexusSearchFilters
        return NexusSearchFilters(
            tag_includes=tuple(self._tag_includes),
            tag_excludes=tuple(self._tag_excludes),
            languages=tuple(self._languages),
            hide_translations=self._hide_translations,
            min_file_size_kb=self._min_size_kb,
            max_file_size_kb=self._max_size_kb,
            min_downloads=self._min_downloads,
            max_downloads=self._max_downloads,
            min_endorsements=self._min_endorsements,
            max_endorsements=self._max_endorsements,
            adult=self._adult,
            supports_vortex=self._supports_vortex,
            has_updated=self._has_updated,
            title_contains=self._title_contains or None,
            description_contains=self._description_contains or None,
            author_contains=self._author_contains or None,
            uploader_contains=self._uploader_contains or None,
        )

    # -- toolbar handlers ---------------------------------------------------
    def _on_sort_changed(self, label: str):
        self._sort_key = dict(SORT_KEYS).get(label, "downloads")
        self._page = 0
        self._reload()

    def _on_time_changed(self, label: str):
        # A preset was chosen - drop any lingering custom range so it doesn't
        # stay pinned in the dropdown next to the presets.
        if self._custom_time_label and label != self._custom_time_label:
            self._custom_time_label = None
            self._time_sel.set_items(
                [lbl for lbl, _ in TIME_RANGES], current=label)
        self._time_days = dict(TIME_RANGES).get(label)
        self._page = 0
        self._reload()

    def _pick_custom_time(self):
        """Open a borderless calendar overlay (like the colour picker) and filter
        to mods uploaded since the picked date. The Nexus filter is one-ended
        ('createdAt >= cutoff'), so a single 'since' date is converted to a
        day-count and reuses the preset path."""
        from gui_qt.date_picker_overlay import DatePickerOverlay
        today = QDate.currentDate()
        initial = self._custom_time_date or today.addMonths(-3)
        DatePickerOverlay.show_over(
            self, self.tr("Uploaded since…"), initial, today,
            self._on_custom_time_picked)

    def _on_custom_time_picked(self, picked: "QDate | None"):
        if picked is None:               # cancelled / Esc / backdrop click
            return
        today = QDate.currentDate()
        if not picked.isValid() or picked > today:
            return
        days = picked.daysTo(today)          # 0 = today (still valid; > 0 path)
        self._custom_time_date = picked
        iso = picked.toString("yyyy-MM-dd")
        label = self.tr("Since {0}").format(iso)
        self._custom_time_label = label
        # Inject the custom label as a selectable item so the button can show it
        # (set_current only accepts known items), then make it current.
        items = [lbl for lbl, _ in TIME_RANGES] + [label]
        self._time_sel.set_items(items, current=label)
        # daysTo(today)==0 (today picked) would disable the filter in the API
        # (created_since_days>0 guard); clamp to 1 day so "today" still filters.
        self._time_days = max(days, 1)
        self._page = 0
        self._reload()

    def _save_adult_pref(self):
        """Persist the current adult choice."""
        # Single writer for both adult checkboxes - persisting from only one of
        # them let the stored preference drift out of sync with the UI. The
        # stored flag is the legacy "show adult" boolean: only an explicit
        # "Hide adult content" counts as hidden.
        try:
            from Utils.ui_config import save_nexus_show_adult
            save_nexus_show_adult(self._adult is not False)
        except Exception:
            pass

    def _on_hide_adult_toggled(self):
        """Apply the "Hide adult content" checkbox."""
        # Hide/Show-only adult are mutually exclusive (the site behaves the
        # same even though it renders them as two independent checkboxes).
        on = bool(self._hide_adult_cb.state())
        if on:
            self._show_only_adult_cb.set_state(0)   # emit=False: no double reload
        self._adult = False if on else None
        self._save_adult_pref()
        self._page = 0
        self._reload()

    def _on_show_only_adult_toggled(self):
        """Apply the "Show only adult content" checkbox."""
        on = bool(self._show_only_adult_cb.state())
        if on:
            self._hide_adult_cb.set_state(0)
        self._adult = True if on else None
        self._save_adult_pref()
        self._page = 0
        self._reload()

    # -- search -------------------------------------------------------------
    def _on_search_mode_changed(self, label: str):
        # Map the (possibly translated) label back to a canonical key.
        mode = "Author" if label == self._mode_label_author else "Name"
        if mode == self._search_mode:
            return
        self._search_mode = mode
        # Switching mode is a fresh, name-based query - drop any pinned uploader
        # id from a prior right-click "Mods by this author".
        self._uploader_id = 0
        self._search.setPlaceholderText(
            self.tr("Search by author…") if mode == "Author"
            else self.tr("Search mods…"))
        # Re-run the current query under the new mode. _do_search_now would
        # short-circuit (query unchanged), so reload directly when there's text.
        q = self._search.text().strip()
        if q and (q.isdigit() or len(q) >= 2):
            self._query = q
            self._page = 0
            self._set_section_for_search()
            self._reload()

    def _clear_search(self):
        """Empty the search field, switch back to Name mode (if the dropdown was
        on Author), and - if a search was active - return to the default
        (unfiltered) listing for the current section."""
        self._search.blockSignals(True)
        self._search.clear()
        self._search.blockSignals(False)
        self._reset_search_mode()   # → Name mode + clears the pinned uploader id
        if self._query:
            self._query = ""
            self._page = 0
            self._reload()

    def _reset_search_mode(self):
        """Return the search bar to Name mode (used when switching sections)."""
        self._uploader_id = 0
        if self._search_mode != "Name":
            self._search_mode = "Name"
            self._search.setPlaceholderText(self.tr("Search mods…"))
        self._mode_sel.set_current(self._mode_label_name)

    def _on_search_text(self, text: str):
        t = getattr(self, "_search_timer", None)
        if t is None:
            t = QTimer(self)
            t.setSingleShot(True)
            t.setInterval(450)
            t.timeout.connect(self._do_search_now)
            self._search_timer = t
        t.start()

    def _do_search_now(self):
        q = self._search.text().strip()
        # Nexus' WILDCARD name filter rejects text terms shorter than 2 chars.
        # Numeric mod-id lookups use a separate path, so single digits are fine.
        if q and not q.isdigit() and len(q) < 2:
            return
        if q == self._query:
            return
        # A manually edited query is name-based - drop any pinned uploader id.
        self._uploader_id = 0
        self._query = q
        self._page = 0
        self._set_section_for_search()
        self._reload()

    def _set_section_for_search(self):
        # Searching only applies to Browse; switch there if elsewhere.
        if self._section != "Browse":
            self._section = "Browse"
            self._update_section_buttons()
            self._update_browse_controls_visibility()

    # -- pagination ---------------------------------------------------------
    def _page_size(self) -> int:
        return int(self._page_size_choice)

    def _on_page_size_changed(self, label: str):
        try:
            size = int(label)
        except (TypeError, ValueError):
            return
        if size == self._page_size_choice:
            return
        self._page_size_choice = size
        self._page = 0
        try:
            from Utils.ui_config import save_nexus_page_size
            save_nexus_page_size(size)
        except Exception:
            pass
        self._reload()

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
    def _on_domain_changed(self, domain: str):
        if (domain or "").strip().lower() == self._domain:
            return
        self._retarget_domain(domain)

    def _retarget_domain(self, domain: str):
        """Reset domain-scoped state and fetch the selected Nexus game."""
        # Pending browser-download watches would install into the NEW game's
        # modlist - stop them (and their progress cards) instead.
        self._cancel_manual_watches()
        # Thumbnail completion signals only carry a numeric mod id. Replace the
        # loader so an in-flight image for the previous domain cannot land on a
        # same-id card in the newly selected domain.
        try:
            self._thumbs.loaded.disconnect(self._on_thumb)
        except (RuntimeError, TypeError):
            pass
        self._thumbs.deleteLater()
        self._thumbs = ThumbnailLoader(self)
        self._thumbs.loaded.connect(self._on_thumb)
        self._domain = (domain or "").strip().lower()
        # Reset navigation + search + filter state to the new game's defaults.
        self._section = "Browse"
        self._page = 0
        self._query = ""
        self._selected_categories = []
        self._time_days = None
        # Drop any custom "Since <date>" range and restore the preset list.
        if self._custom_time_label:
            self._custom_time_label = None
            self._custom_time_date = None
            self._time_sel.set_items(
                [lbl for lbl, _ in TIME_RANGES], current="All time")
        self._fetch_token += 1              # invalidate any in-flight fetch
        # Clear the search box without re-triggering a search.
        try:
            self._search.blockSignals(True)
            self._search.clear()
            self._search.blockSignals(False)
        except Exception:
            pass
        self._reset_search_mode()
        # Force categories to reload for the new game.
        self._cats_loaded = False
        while self._cat_layout.count():
            it = self._cat_layout.takeAt(0)
            w = it.widget()
            if w is not None:
                w.deleteLater()
        self._cat_checks.clear()
        # Advanced search filters (tags especially) are game-specific - reset
        # them and force the tag list to reload for the new game.
        self._tags_loaded = False
        self._game_tags = []
        self._reset_advanced_filter_state()
        self._update_section_buttons()
        self._update_browse_controls_visibility()
        self._load_categories()
        self._load_tags()
        self._reload()

    def set_game(self, game, domain):
        """Retarget the browser after the application changes games."""
        self._game = game
        domains = tuple(getattr(game, "nexus_game_domains", ()) or ())
        self._domains = domains or tuple(
            d for d in (((domain or "").strip().lower()),) if d)
        primary = self._domains[0] if self._domains else ""
        self._domain_sel.set_items(list(self._domains), current=primary)
        self._domain_sel.setVisible(len(self._domains) > 1)
        self._retarget_domain(primary)

    def _reload(self):
        if not self._domain:
            self._status.setText(self.tr("No Nexus domain for this game."))
            return
        self._fetch_token += 1
        token = self._fetch_token
        self._set_loading(True)
        section = self._section
        page = self._page
        size = self._page_size()
        sort_key = self._sort_key
        time_days = self._time_days
        query = self._query
        mode = self._search_mode
        uploader_id = self._uploader_id
        cats = list(self._selected_categories) or None
        domain = self._domain
        # Advanced filters only apply to the paged listing/search calls below
        # (Tracked/Endorsed fetch by id list, not a ModsFilter search - same
        # scoping the existing category filter already has).
        filters = self._current_search_filters()

        def worker():
            entries = []
            status = ""
            try:
                if section == "Browse" and query:
                    if mode == "Author":
                        if uploader_id:
                            # Right-click path: reliable stable-id lookup.
                            entries = self._api.search_mods_by_uploader_id(
                                domain, uploader_id, count=size,
                                offset=page * size, category_names=cats,
                                sort_key=sort_key, filters=filters)
                        else:
                            # Typed author search: match uploader by name.
                            entries = self._api.search_mods_by_author(
                                domain, query, count=size, offset=page * size,
                                category_names=cats, sort_key=sort_key,
                                filters=filters)
                        status = f"Mods by '{query}': page {page + 1} ({len(entries)} result(s))"
                    elif query.isdigit():
                        entries = self._api.search_mod_by_id(domain, int(query))
                        status = f"Search '{query}': page {page + 1} ({len(entries)} result(s))"
                    else:
                        entries = self._api.search_mods(
                            domain, query, count=size, offset=page * size,
                            category_names=cats, sort_key=sort_key,
                            filters=filters)
                        status = f"Search '{query}': page {page + 1} ({len(entries)} result(s))"
                elif section == "Browse":
                    entries = self._api.get_top_mods(
                        domain, count=size, offset=page * size,
                        category_names=cats, created_since_days=time_days,
                        sort_key=sort_key, filters=filters)
                    status = f"Browse: page {page + 1}"
                elif section == "Trending":
                    entries = self._api.get_trending_mods_graphql(
                        domain, count=size, offset=page * size,
                        category_names=cats, filters=filters)
                    status = f"Trending (7 days): page {page + 1}"
                elif section == "Tracked":
                    entries = self._fetch_user_mods(domain, self._api.get_tracked_mods)
                    status = f"Tracked: {len(entries)} mod(s)"
                elif section == "Endorsed":
                    entries = self._fetch_user_mods(
                        domain, self._api.get_endorsements, only_status="Endorsed")
                    status = f"Endorsed: {len(entries)} mod(s)"
            except Exception as exc:
                self._log(f"Nexus: fetch error: {exc}")
                status = f"Error: {exc}"
                entries = []
            safe_emit(self._results_ready, entries, status, token)

        threading.Thread(target=worker, daemon=True).start()

    def _fetch_user_mods(self, domain, list_fn, only_status=None):
        """Tracked/Endorsed: list dicts → filter to this game → batch mod info."""
        rows = list_fn() or []
        ids = []
        for r in rows:
            if (r.get("domain_name", "") or "").lower() != domain.lower():
                continue
            if only_status and r.get("status", "") != only_status:
                continue
            mid = r.get("mod_id", 0)
            if mid:
                ids.append((domain, int(mid)))
        if not ids:
            return []
        info_map = self._api.graphql_mod_info_batch(ids)
        out = [info_map[mid] for _d, mid in ids if mid in info_map]
        return out

    def _on_results(self, entries, status, token):
        if token != self._fetch_token:
            return                       # stale
        self._entries = list(entries or [])
        # Some REST/GraphQL shapes omit the slug. Stamp the domain captured by
        # this accepted result so later file fetches and metadata never fall
        # back to a different selector value.
        for entry in self._entries:
            if not (getattr(entry, "domain_name", "") or "").strip():
                try:
                    entry.domain_name = self._domain
                except Exception:
                    pass
        self._status.setText(status)
        self._page_edit.setText(str(self._page + 1))
        self._set_loading(False)
        self._rebuild_cards()
        self._scroll.verticalScrollBar().setValue(0)
        self._update_page_buttons()

    def _set_loading(self, on: bool):
        for w in (self._prev_btn, self._next_btn, self._sort_sel, self._time_sel,
                  self._domain_sel,
                  self._page_edit, self._perpage_sel):
            w.setEnabled(not on)
        if on:
            self._status.setText(self.tr("Loading…"))
            self._loading_overlay.show_over()
        else:
            self._loading_overlay.hide_overlay()

    def _update_page_buttons(self):
        paged = self._section in ("Browse", "Trending")
        self._prev_btn.setEnabled(paged and self._page > 0)
        self._next_btn.setEnabled(paged and len(self._entries) >= self._page_size())

    # -- cards / grid -------------------------------------------------------
    def _visible_entries(self):
        """Client-side adult filter for Tracked/Endorsed."""
        # Browse/Trending/search already filter server-side via
        # NexusSearchFilters.adult - re-filtering them here would be a no-op at
        # best and, if a node ever came back without adultContent populated,
        # would silently empty the page. Tracked/Endorsed fetch by id list, so
        # they get no ModsFilter and still need this pass.
        if self._adult is None or self._section not in ("Tracked", "Endorsed"):
            return self._entries
        want_adult = self._adult
        return [e for e in self._entries
                if bool(getattr(e, "contains_adult_content", False)) == want_adult]

    def _installed_ids(self) -> set:
        """Nexus mod IDs already installed in the active profile's staging for
        this game's domain. Recomputed each card rebuild / profile change."""
        game = self._game
        if game is None or not getattr(game, "is_configured", lambda: False)():
            return set()
        try:
            from pathlib import Path
            from Nexus.nexus_meta import scan_installed_mods
            staging = game.get_effective_mod_staging_path()
            if not staging or not Path(staging).is_dir():
                return set()
            domain = (self._domain or "").lower()
            primary = (getattr(game, "nexus_game_domain", "") or "").lower()
            return {
                m.mod_id for m in scan_installed_mods(Path(staging))
                if m.mod_id > 0
                and (not domain
                     or ((m.game_domain or "").lower() or primary) == domain)
            }
        except Exception:
            return set()

    def _rebuild_cards(self):
        for c in self._cards:
            c.setParent(None)
        self._cards.clear()
        installed = self._installed_ids()
        dl_only = self._download_only()      # one ini read for the whole grid, not per card
        for e in self._visible_entries():
            card = NexusModCard(e, self._on_view, self._on_install,
                                on_context=self._show_card_menu,
                                is_installed=e.mod_id in installed,
                                download_only=dl_only)
            if e.mod_id in self._manual_watchers:
                card.set_watching(True)
            self._cards.append(card)
            self._thumbs.request(e.mod_id, getattr(e, "picture_url", "") or "")
        self._cols = 0
        self._relayout()

    def refresh_installed(self):
        """Recompute installed IDs and flip card buttons. Call on profile change
        and after an install completes - the browser tab persists across both."""
        installed = self._installed_ids()
        for card in self._cards:
            card.set_installed(card.entry.mod_id in installed)

    @staticmethod
    def _download_only() -> bool:
        from Utils.ui_config import load_download_only
        try:
            return bool(load_download_only())
        except Exception:
            return False

    def refresh_install_labels(self):
        """Re-read 'Download only' and relabel the card buttons."""
        # The right-click menu is rebuilt per click, so it needs nothing here.
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
        # Center the row group: cards live in columns 1..cols, and equal-stretch
        # spacer columns on both sides (0 and cols+1) push the block to center.
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
    def _mod_url(self, entry) -> str:
        dom = getattr(entry, "domain_name", "") or self._domain
        return f"https://www.nexusmods.com/{dom}/mods/{entry.mod_id}"

    def _on_view(self, entry):
        from Utils.xdg import open_url
        open_url(self._mod_url(entry), log_fn=self._log)

    def _show_card_menu(self, entry, global_pos):
        menu = QMenu(self)
        menu.addAction(self.tr("Open on Nexus"), lambda: self._on_view(entry))
        # _on_install toggles: while a browser-download watch is pending for
        # this mod, the same action cancels it instead.
        _act = (self.tr("Download") if self._download_only()
                else self.tr("Install"))
        menu.addAction(
            self.tr("Cancel download detection")
            if entry.mod_id in self._manual_watchers else _act,
            lambda: self._on_install(entry))
        # Browse by the uploader's stable account id (reliable - survives a
        # rename and can't be spoofed via the free-text `author` field). Fall
        # back to the display name / author only for a label when no id is
        # available (e.g. a REST-sourced entry without uploader.memberId).
        uploader_id = int(getattr(entry, "uploader_id", 0) or 0)
        uploader_name = (getattr(entry, "uploaded_by", "") or "").strip() \
            or (getattr(entry, "author", "") or "").strip()
        if uploader_id or uploader_name:
            menu.addAction(
                self.tr("Mods by this author"),
                lambda: self._browse_author(uploader_name, uploader_id))
        if self._section == "Tracked":
            menu.addAction(self.tr("Untrack"), lambda: self._user_action(
                "untrack", entry))
        else:
            menu.addAction(self.tr("Track Mod"), lambda: self._user_action(
                "track", entry))
        if self._section == "Endorsed":
            menu.addAction(self.tr("Abstain"), lambda: self._user_action(
                "abstain", entry))
        menu.exec(global_pos)

    def _browse_author(self, author: str, uploader_id: int = 0):
        """List all mods by an uploader in the Browse section. Triggered from a
        card's right-click menu. When *uploader_id* is given, search uses the
        stable account id; *author* is only the human-readable label shown in
        the search field."""
        author = (author or "").strip()
        uploader_id = int(uploader_id or 0)
        if not author and not uploader_id:
            return
        # Switch to Browse first - _set_section clears the query and resets the
        # mode (incl. the pinned uploader id), so do it before we set up below.
        if self._section != "Browse":
            self._set_section("Browse")
        # Enter Author mode and run the search. Pin the uploader id so the worker
        # takes the reliable id path rather than the typed-name path.
        self._search_mode = "Author"
        self._uploader_id = uploader_id
        self._mode_sel.set_current(self._mode_label_author)
        self._search.setPlaceholderText(self.tr("Search by author…"))
        self._search.blockSignals(True)
        self._search.setText(author)
        self._search.blockSignals(False)
        # Use a non-empty query so the worker enters the search branch even when
        # only an id is known (fall back to the id string as a last resort).
        self._query = author or str(uploader_id)
        self._page = 0
        self._reload()

    def _user_action(self, kind: str, entry):
        domain = getattr(entry, "domain_name", "") or self._domain
        mod_id = entry.mod_id

        def worker():
            try:
                if kind == "track":
                    self._api.track_mod(domain, mod_id)
                    self._log(f"Nexus: tracking {entry.name}")
                elif kind == "untrack":
                    self._api.untrack_mod(domain, mod_id)
                    self._log(f"Nexus: untracked {entry.name}")
                elif kind == "abstain":
                    self._api.abstain_mod(domain, mod_id,
                                          getattr(entry, "version", ""))
                    self._log(f"Nexus: abstained {entry.name}")
            except Exception as exc:
                self._log(f"Nexus: {kind} error: {exc}")

        threading.Thread(target=worker, daemon=True).start()

    # -- install (premium check → file pick → download → install queue) ----
    def _on_install(self, entry):
        if entry.mod_id in self._manual_watchers:
            # Waiting for this mod's browser download - the click cancels the
            # watch (e.g. it can't recognise the file). Click again to retry.
            self.cancel_manual_watch(entry.mod_id)
            self._log(f"Nexus: cancelled download detection for "
                      f"{entry.name or entry.mod_id}.")
            return
        if self._installing:
            self._log("Nexus: an install is already in progress.")
            return
        self._installing = True
        mod_id = entry.mod_id
        name = entry.name or f"Mod {mod_id}"
        self._log(f"Nexus: preparing install for {name}…")

        def _check_premium():
            premium = bool(self._api.validate().is_premium)
            if premium:
                # [dev] force_manual_install = true → exercise the manual
                # browser-download flow (same switch the collections use).
                from Utils.ui_config import load_force_manual_install
                if load_force_manual_install():
                    self._log("Nexus: [dev] force_manual_install - using the "
                              "manual browser-download flow.")
                    premium = False
            return (entry, premium)

        run_in_worker(_check_premium,
                      self._premium_checked, name="nexus-premium-check",
                      unpack=True, error_result=(entry, None))

    def _on_premium_checked(self, entry, is_premium):
        domain = getattr(entry, "domain_name", "") or self._domain
        mod_id = entry.mod_id
        if not is_premium:
            # Non-premium (or unknown): the user downloads on the site. Fetch
            # the file list first so a folder watcher can recognise the archive
            # and auto-install it when it lands (manual collection installer
            # parity); the files page opens once the list is in.
            run_in_worker(
                lambda: (entry, list(self._api.get_mod_files(domain, mod_id).files)),
                self._manual_files_ready, name="nexus-manual-files",
                unpack=True, error_result=(entry, None))
            return
        # Premium: fetch the file list on a worker → back to UI for the chooser.
        run_in_worker(
            lambda: (entry, list(self._api.get_mod_files(domain, mod_id).files)),
            self._files_ready, name="nexus-file-list",
            unpack=True, error_result=(entry, []))

    # -- non-premium: file chooser → file's download page + folder watch ----
    def _on_manual_files_ready(self, entry, files):
        """UI thread (non-premium install): pick the file (same chooser as the
        premium path), open ITS download page directly, and watch the download
        folders so the browser download auto-installs when it arrives.
        'Download with Mod Manager' still works too - that comes back as an
        nxm:// link, which cancels this watch (see app._handle_nxm)."""
        from gui_qt.nexus_file_chooser import NexusFileChooser, installable_files
        picks = installable_files(files or [])
        if not picks:
            # No file list (API error) or nothing installable: fall back to
            # the plain files page - no watch possible without expected files.
            from Utils.xdg import open_url
            self._installing = False
            open_url(f"{self._mod_url(entry)}?tab=files", log_fn=self._log)
            self._log("Nexus: premium required for direct download - opened "
                      "the files page (couldn't fetch the file list, so the "
                      "download won't auto-install; use 'Download with Mod "
                      "Manager' or the Downloads tab).")
            return
        if len(picks) > 1:
            def _picked(chosen):
                if chosen is None:
                    self._log("Nexus: install cancelled.")
                    self._installing = False
                    return
                self._open_manual_file(entry, chosen)

            NexusFileChooser.show_over(
                self, entry.name or f"Mod {entry.mod_id}", picks, _picked)
        else:
            self._open_manual_file(entry, picks[0])

    def _open_manual_file(self, entry, file):
        """Install the chosen file via the shared start_manual_install flow:
        skip the browser when the archive is already downloaded, else open the
        file's download page (file_id deep-link) + watch the download folders."""
        from Nexus.manual_download_watch import start_manual_install
        from Utils.xdg import open_url
        self._installing = False
        domain = getattr(entry, "domain_name", "") or self._domain
        mod_id = entry.mod_id
        name = entry.name or f"Mod {mod_id}"
        self.cancel_manual_watch(mod_id)    # re-click → fresh watch
        self._dl_seq += 1
        dl_key = f"nxb-man-{self._dl_seq}"
        # Indeterminate card while waiting; switches to real bytes once the
        # watcher can see the in-flight browser download.
        self._progress_fn(dl_key, name, 0, 0)
        watchers = self._manual_watchers

        # All three callbacks run on the WATCHER thread - marshal via Signals.
        def _claim() -> bool:
            # Only the watch that still owns the slot may emit completion -
            # a watch cancelled or replaced by a re-click must stay silent
            # (double install / clobbering the new watch's progress card).
            t = watchers.pop(mod_id, None)
            if t is None:
                return False
            if t[1] != dl_key:
                watchers[mod_id] = t   # a newer watch owns the slot
                return False
            return True

        def on_archive(path, meta, _file):
            if not _claim():
                return
            safe_emit(self._download_done, str(path), meta, dl_key)
            safe_emit(self._manual_watch_ended, mod_id)

        def on_progress(done, total):
            safe_emit(self._download_progress, dl_key, name, int(done), int(total))

        def on_timeout():
            if not _claim():
                return
            self._log(f"Nexus: stopped waiting for a browser download of "
                      f"{name} (nothing arrived - install it from the "
                      f"Downloads tab once downloaded).")
            safe_emit(self._download_done, None, None, dl_key)
            safe_emit(self._manual_watch_ended, mod_id)

        watcher, _already = start_manual_install(
            api=self._api, game_domain=domain, mod_id=mod_id, files=[file],
            open_url_fn=lambda u: open_url(u, log_fn=self._log),
            log_fn=self._log, log_label=file.file_name,
            mod_info=entry,          # card entry IS the NexusModInfo - no fetch
            on_archive=on_archive, on_progress=on_progress,
            on_timeout=on_timeout)
        watchers[mod_id] = (watcher, dl_key)
        self._set_card_watching(mod_id, True)

    def cancel_manual_watch(self, mod_id: int, game_domain: str = ""):
        """Stop a pending browser-download watch (no-op if none). Called on
        re-click, and by the app when an nxm:// download for the same mod
        arrives - the nxm flow installs it, so the watch must not."""
        if (game_domain and
                str(game_domain).strip().lower() != self._domain):
            return
        t = self._manual_watchers.pop(int(mod_id or 0), None)
        if t is not None:
            watcher, dl_key = t
            watcher.stop()
            self._progress_fn(dl_key, "", 0, -1)
            self._set_card_watching(int(mod_id or 0), False)

    def _cancel_manual_watches(self):
        for mod_id in list(self._manual_watchers):
            self.cancel_manual_watch(mod_id)

    def _on_manual_watch_ended(self, mod_id: int):
        self._set_card_watching(mod_id, False)

    def _set_card_watching(self, mod_id: int, watching: bool):
        for card in self._cards:
            if card.entry.mod_id == mod_id:
                card.set_watching(watching)

    def _on_files_ready(self, entry, files):
        """UI thread: pick the file to install. Main/optional/misc; >1 → chooser."""
        from gui_qt.nexus_file_chooser import NexusFileChooser, installable_files
        picks = installable_files(files)
        if not picks:
            self._log("Nexus: no downloadable files found.")
            self._installing = False
            return
        if len(picks) > 1:
            def _picked(chosen):
                if chosen is None:
                    self._log("Nexus: install cancelled.")
                    self._installing = False
                    return
                self._start_download(entry, chosen)

            NexusFileChooser.show_over(
                self, entry.name or f"Mod {entry.mod_id}", picks, _picked)
        else:
            self._start_download(entry, picks[0])

    def _start_download(self, entry, file):
        domain = getattr(entry, "domain_name", "") or self._domain
        name = entry.name or f"Mod {entry.mod_id}"
        dl_label = file.file_name or name
        self._dl_seq += 1
        dl_key = f"nxb-{self._dl_seq}"
        self._log(f"Nexus: downloading {dl_label}…")
        cancel = threading.Event()
        # Show the popup immediately (indeterminate) so there's feedback even
        # before the first progress callback arrives.
        self._progress_fn(dl_key, dl_label, 0, 0, cancel.set)

        def worker():
            archive = None
            meta = None
            try:
                from Nexus.nexus_download import NexusDownloader
                from Utils.config_paths import get_download_cache_dir_for_game
                # Download into the per-game CACHE folder (the Downloads tab
                # scans this), matching the Tk Nexus browser - NOT ~/Downloads.
                dest = get_download_cache_dir_for_game(
                    getattr(self._game, "name", "") or "")
                size = (file.size_in_bytes or 0) or (file.size_kb * 1024)
                result = NexusDownloader(self._api, download_dir=dest).download_file(
                    game_domain=domain, mod_id=entry.mod_id, file_id=file.file_id,
                    dest_dir=dest, known_file_name=file.file_name,
                    expected_size_bytes=size,
                    progress_cb=lambda d, t: safe_emit(
                        self._download_progress, dl_key, dl_label, int(d), int(t)),
                    cancel=cancel)
                if result.success and result.file_path is not None:
                    archive = str(result.file_path)
                    # Build the meta from the KNOWN mod_id/file_id (the archive
                    # name can mis-parse), like the Tk browser - so the installed
                    # meta.ini records the right id and Reinstall detection works.
                    try:
                        from Nexus.nexus_meta import build_meta_from_download
                        meta = build_meta_from_download(
                            game_domain=domain, mod_id=entry.mod_id,
                            file_id=file.file_id, archive_name=result.file_name,
                            mod_info=entry, file_info=file)
                    except Exception:
                        meta = None
                else:
                    if "cancel" in (result.error or "").lower():
                        self._log("Nexus: download cancelled.")
                    else:
                        self._log(f"Nexus: download failed: "
                                  f"{result.error or 'unknown error'}")
            except Exception as exc:
                self._log(f"Nexus: download error: {exc}")
            safe_emit(self._download_done, archive, meta, dl_key)

        threading.Thread(target=worker, daemon=True).start()
        # The download is underway on its own thread with its own progress
        # card - release the guard so the user can queue up the next mod
        # while this one downloads/installs (installs serialise in the app's
        # pending-install queue).
        self._installing = False

    def _on_download_progress(self, key, name, downloaded, total):
        """UI thread: forward download bytes to this download's progress card."""
        self._progress_fn(key, name, downloaded, total)

    def _on_download_done(self, archive, meta, dl_key):
        """UI thread: hand the downloaded archive (+ its prebuilt meta) to the
        app's install queue."""
        # Hide this download's card (the install queue shows its own progress).
        self._progress_fn(dl_key, "", 0, -1)
        if not archive:
            return
        self._log(f"Nexus: downloaded → {archive}"
                  f"{'' if self._download_only() else '; installing…'}")
        self._install_fn([archive], {archive: meta} if meta is not None else None)
