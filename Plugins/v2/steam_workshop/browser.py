"""Steam Workshop browser using anonymous SteamKit queries."""

from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
import re
import threading
import time

from PySide6.QtCore import QCoreApplication, QDate, QEvent, Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QFrame, QGridLayout, QHBoxLayout, QLabel, QLineEdit,
    QMenu, QPushButton, QScrollArea, QSizePolicy, QSplitter,
    QTabWidget, QToolButton, QVBoxLayout, QWidget,
)

from .client import WorkshopClient, installed_items, parse_entry
from .support import supports_workshop_install, workshop_app_id
from .download import DownloadCancelled, saved_account, state_root
from gui_qt.widgets.flow_layout import FlowLayout
from gui_qt.nexus.mod_card import NexusModCard
from gui_qt.widgets.card_helpers import CARD_W, ThumbnailLoader
from gui_qt.runtime.safe_emit import safe_emit


@dataclass
class _Card:
    entry: object
    mod_id: int
    name: str
    summary: str
    picture_url: str
    category_name: str
    updated_at: str
    created_at: str
    file_size_kb: int
    endorsement_count: int
    downloads_total: int
    author: str = ""


class WorkshopModCard(NexusModCard):
    def __init__(self, entry, on_view, on_install, on_subscribe, **kwargs):
        super().__init__(entry, on_view, on_install, **kwargs)
        layout = self._install_btn.parentWidget().layout()
        row = layout.itemAt(layout.count() - 1).layout()
        self._view_button = row.takeAt(0).widget()
        layout.insertWidget(layout.count() - 1, self._view_button)
        self._subscribe_button = QPushButton(self.tr('Subscribe'))
        self._subscribe_button.setObjectName('GameAddBtn')
        self._subscribe_button.setCursor(Qt.PointingHandCursor)
        self._subscribe_button.clicked.connect(lambda: on_subscribe(entry.entry))
        row.addWidget(self._subscribe_button, 1)


class _Details(QWidget):
    def __init__(self, host, entry, install_fn, close_fn, installed=False):
        super().__init__(host)
        self.setObjectName('WorkshopDetails')
        self.setAttribute(Qt.WA_StyledBackground, True)
        from gui_qt.appearance.theme import active_palette, _c
        from Utils.downloads.core import fmt_size
        self.setStyleSheet(f"#WorkshopDetails {{ background:{_c(active_palette(), 'BG_MAIN')}; }}")
        self._entry = entry
        self._installed = installed
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        bar = QHBoxLayout()
        back = QPushButton(self.tr('◂ Back')); back.setObjectName('FormButton')
        back.clicked.connect(close_fn); bar.addWidget(back)
        title = QLabel(entry.full_name); title.setTextFormat(Qt.PlainText)
        title.setWordWrap(True); title.setStyleSheet('font-size:18px; font-weight:600;')
        bar.addWidget(title, 1)
        open_page = QPushButton(self.tr('Open on Steam Workshop'))
        open_page.setObjectName('FormButton')
        open_page.setToolTip(self.tr('Subscribe, favorite or vote on Steam.'))
        open_page.clicked.connect(lambda: _open_url(entry.item.url)); bar.addWidget(open_page)
        self._subscribe_button = QPushButton()
        self._subscribe_button.setObjectName('FormButton')
        self._subscribe_button.setVisible(host._subscribe is not None)
        self._subscribe_button.clicked.connect(lambda: host._toggle_subscription(entry))
        host._subscription_button(self._subscribe_button, entry)
        bar.addWidget(self._subscribe_button)
        self._install_button = QPushButton()
        self._install_button.setObjectName('PrimaryButton')
        self._install_button.setEnabled(install_fn is not None)
        self._install_button.clicked.connect(lambda: install_fn(entry) if install_fn else None)
        bar.addWidget(self._install_button); layout.addLayout(bar)
        stats = QLabel(self.tr('{0} · {1} subscribers · {2} votes · {3} favorites · {4} views').format(
            fmt_size(entry.file_size), entry.subscriptions, entry.votes, entry.favorites, entry.views))
        stats.setWordWrap(True); stats.setObjectName('Help'); layout.addWidget(stats)
        if entry.creator:
            author = QPushButton(self.tr('Open author profile'))
            author.setObjectName('FormButton')
            author.clicked.connect(lambda: _open_url(f'https://steamcommunity.com/profiles/{entry.creator}'))
            layout.addWidget(author, 0, Qt.AlignLeft)
        tabs = QTabWidget(); layout.addWidget(tabs, 1)
        from gui_qt.nexus.mod_detail import _DescriptionBrowser
        self._description = _DescriptionBrowser()
        self._description.setOpenLinks(False)
        self._description.setOpenExternalLinks(False)
        self._description.anchorClicked.connect(self._description_link)
        self._spoilers = set()
        self._render_description()
        tabs.addTab(self._description, self.tr('Description'))
        self._images = list(dict.fromkeys(url for url in (entry.preview, *entry.previews)
                                         if url.startswith(('https://', 'http://'))))
        self._image_index = 0
        self._image_loader = ThumbnailLoader(self, crop_w=1100, crop_h=650, fit=True)
        self._image_loader.loaded.connect(self._image_ready)
        self._image = QLabel(self.tr('No images available.')); self._image.setAlignment(Qt.AlignCenter)
        self._image.setMinimumSize(1, 1)
        self._image.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Ignored)
        self._image.installEventFilter(self)
        images = QWidget(); image_layout = QVBoxLayout(images); image_layout.addWidget(self._image, 1)
        controls = QHBoxLayout()
        previous = QPushButton(self.tr('◂ Previous')); previous.clicked.connect(lambda: self._show_image(-1))
        following = QPushButton(self.tr('Next ▸')); following.clicked.connect(lambda: self._show_image(1))
        self._image_count = QLabel(); self._image_count.setAlignment(Qt.AlignCenter)
        controls.addWidget(previous); controls.addWidget(self._image_count, 1); controls.addWidget(following)
        for button in (previous, following):
            button.setObjectName('FormButton'); button.setEnabled(len(self._images) > 1)
        image_layout.addLayout(controls); tabs.addTab(images, self.tr('Images'))
        requirements = QScrollArea(); requirements.setWidgetResizable(True)
        requirements_body = QWidget(); required = QVBoxLayout(requirements_body)
        note = QLabel(self.tr('Workshop requirements are resolved automatically when downloading.'))
        note.setWordWrap(True); required.addWidget(note)
        for item in entry.children:
            button = QPushButton(self.tr('Workshop item {0} ↗').format(item)); button.setObjectName('FormButton')
            button.clicked.connect(lambda _=False, item_id=item: _open_url(
                f'https://steamcommunity.com/sharedfiles/filedetails/?id={item_id}'))
            required.addWidget(button)
        if not entry.children: required.addWidget(QLabel(self.tr('No Workshop requirements listed.')))
        required.addStretch(); requirements.setWidget(requirements_body)
        tabs.addTab(requirements, self.tr('Requirements ({0})').format(len(entry.children)))
        self._pixmap = None
        self._show_image(0)
        self.refresh_install_labels()
        self.setGeometry(host.rect()); self.show(); self.raise_()

    def refresh_install_labels(self):
        self._install_button.setText(self.tr('Download') if self.parentWidget()._download_only() else
                                     self.tr('Reinstall') if self._installed else self.tr('Download and install'))

    def _render_description(self):
        from gui_qt.nexus.bbcode import nexus_bbcode_to_html
        self._description.setHtml(nexus_bbcode_to_html(self._entry.description, self._spoilers))

    def _description_link(self, url):
        if url.scheme() == 'nexus-spoiler':
            try: index = int(url.path())
            except ValueError: return
            self._spoilers.symmetric_difference_update({index})
            self._render_description()
        elif url.scheme() in {'http', 'https'}:
            _open_url(url.toString())

    def _show_image(self, step):
        if not self._images: return
        self._image_index = (self._image_index + step) % len(self._images)
        self._image_count.setText(self.tr('{0} / {1}').format(self._image_index + 1, len(self._images)))
        self._pixmap = None; self._image.setText(self.tr('Loading image…'))
        self._image_loader.request(self._image_index, self._images[self._image_index])

    def _image_ready(self, index, pixmap):
        if index == self._image_index:
            self._pixmap = pixmap; self._scale_image()

    def _scale_image(self):
        if self._pixmap is not None:
            self._image.setPixmap(self._pixmap.scaled(self._image.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._scale_image()

    def eventFilter(self, obj, event):
        if obj is self._image and event.type() == QEvent.Resize:
            self._scale_image()
        return super().eventFilter(obj, event)


def _open_url(url):
    from Utils.environment.xdg import open_url
    open_url(url)


class WorkshopBrowserView(QWidget):
    _results = Signal(object, object, str)
    _action_ready = Signal(object, object, object, str)
    _subscriptions_ready = Signal(object, object, str)

    def __init__(self, game, *, context_fn, install_fn, subscribe_fn=None, log_fn=None, toolbar_fn=None, parent=None):
        super().__init__(parent)
        self._game, self._app_id = game, workshop_app_id(game)
        self._context, self._install = context_fn, install_fn
        self._subscribe = subscribe_fn
        self._subscriptions = {}
        self._subscription_scope = None
        self._subscription_client = None
        self._subscription_cancel = threading.Event()
        self._subscriptions_checked = 0
        self._subscription_error = ''
        self._subscribed_client = None
        self._listing_account = ''
        self._subscribed_checked = 0
        self._subscriptions_ready.connect(self._subscriptions_received)
        self.destroyed.connect(self._stop_subscription_check)
        QCoreApplication.instance().aboutToQuit.connect(self._stop_subscription_check)
        self.destroyed.connect(self._stop_subscribed_listing)
        QCoreApplication.instance().aboutToQuit.connect(self._stop_subscribed_listing)
        self._log = log_fn or (lambda _: None)
        self._toolbar_fn = toolbar_fn
        self._client = WorkshopClient()
        self.destroyed.connect(self._client.close)
        QCoreApplication.instance().aboutToQuit.connect(self._client.close)
        self._cancel, self._action_cancel = threading.Event(), threading.Event()
        self._page, self._total = 0, 0
        self._thumbnail_ids = {}
        self._busy, self._preparing = False, False
        self._entries, self._cards = [], []
        self._selected, self._known_tags = {}, set()
        self._selection_context = self._context()
        self._section, self._sort_value, self._days = 'Browse', 9, 0
        self._detail = None
        from .preferences import load_workshop_page_size
        self._page_size = load_workshop_page_size()
        self._query = {}
        self._thumbs = ThumbnailLoader(self)
        self._thumbs.loaded.connect(self._thumbnail)
        self._results.connect(self._received)
        self._action_ready.connect(self._action_received)
        self._build()
        from gui_qt.runtime.mouse_navigation import MouseNavigationFilter
        self._navigation = MouseNavigationFilter(self, self._back, lambda: self._turn(1))
        self._reset()

    def _build(self):
        from gui_qt.nexus.browser import NexusBrowserView
        from gui_qt.widgets.selector_button import SelectorButton
        from gui_qt.dialogs.loading import LoadingOverlay
        from gui_qt.appearance.theme import active_palette
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        toolbar, row = self._bar()
        self._filter_toggle = self._button(row, self.tr('☰ Filters'), self._toggle_filters)
        self._filter_toggle.setCheckable(True)
        self._filter_toggle.setChecked(True)
        self._sections = {}
        for name, label in [('Browse', self.tr('Browse')), ('Trending', self.tr('Trending'))]:
            button = self._button(row, label, lambda _=False, n=name: self._set_section(n))
            button.setCheckable(True)
            button.setChecked(name == self._section)
            self._sections[name] = button
        if self._toolbar_fn is not None:
            self._toolbar_fn(self, row)
        row.add_stretch()
        self._sorts = {self.tr('Subscribers'): 9, self.tr('Date Published'): 1,
                       self.tr('Top rated'): 0, self.tr('Last Updated'): 21, self.tr('Relevance'): 12}
        self._sort = SelectorButton(items=list(self._sorts), current=self.tr('Subscribers'),
                                    prefix=self.tr('Sort: '), min_width=150, on_select=self._sort_changed)
        row.addWidget(self._sort)
        self._times = {self.tr('All time'): 0, self.tr('24 hours'): 1, self.tr('7 days'): 7,
                       self.tr('28 days'): 28, self.tr('3 months'): 90, self.tr('1 year'): 365}
        self._time = SelectorButton(items=list(self._times), current=self.tr('All time'),
                                    prefix=self.tr('Time: '), min_width=130, on_select=self._time_changed,
                                    actions=[(self.tr('Custom…'), self._custom_time)])
        self._time.setToolTip(self.tr('Show mods published within this time period. Trending ranks the last seven days.'))
        row.addWidget(self._time)
        self._button(row, self.tr('Refresh'), self._reload)
        self._install_all = self._button(row, self.tr('Install all'), self._install_visible)
        self._install_all.setToolTip(self.tr('Download all mods shown on this page, including their requirements.'))
        from Utils.ui.config import load_dev_mode
        self._install_all.setVisible(load_dev_mode())
        outer.addWidget(toolbar)

        self._split = QSplitter(Qt.Horizontal)
        self._split.setHandleWidth(6)
        self._filter_panel = QWidget()
        self._filter_panel.setObjectName('FilterPanel')
        self._filter_panel.setMinimumWidth(120)
        self._filter_panel.setMaximumWidth(460)
        panel = QVBoxLayout(self._filter_panel)
        panel.setContentsMargins(0, 0, 0, 0)
        panel.setSpacing(0)
        header = QWidget(); header.setObjectName('FilterHeader')
        header_layout = QHBoxLayout(header); header_layout.setContentsMargins(10, 6, 8, 6)
        title = QLabel(self.tr('Filters')); title.setObjectName('FilterTitle')
        header_layout.addWidget(title)
        panel.addWidget(header)
        rule = QFrame(); rule.setObjectName('FilterRule'); rule.setFixedHeight(1)
        panel.addWidget(rule)
        filter_scroll = QScrollArea(); filter_scroll.setWidgetResizable(True)
        body = QWidget(); body.setObjectName('FilterBody')
        filters = QVBoxLayout(body)
        filters.setContentsMargins(10, 8, 10, 12); filters.setSpacing(6)
        filters.setAlignment(Qt.AlignTop)
        self._tag_layout = self._filter_section(filters, self.tr('Tags'))
        self._tag_note = QLabel(self.tr('Tags found in browsed results. You can also enter any tag below.'))
        self._tag_note.setWordWrap(True); self._tag_note.setObjectName('Help')
        self._tag_layout.addWidget(self._tag_note)
        self._tag_checks = {}
        params = self._filter_section(filters, self.tr('Search Parameters'))
        self._include = QLineEdit(); self._include.setPlaceholderText(self.tr('Include tags, separated by commas'))
        self._exclude = QLineEdit(); self._exclude.setPlaceholderText(self.tr('Exclude tags, separated by commas'))
        for label, entry in [(self.tr('Include tags'), self._include), (self.tr('Exclude tags'), self._exclude)]:
            params.addWidget(QLabel(label)); params.addWidget(entry)
            entry.returnPressed.connect(self._reset)
        self._match_all = QCheckBox(self.tr('Match all included tags')); self._match_all.setChecked(True)
        params.addWidget(self._match_all)
        self._button(params, self.tr('Apply filters'), self._reset)
        subscriptions = self._filter_section(filters, self.tr('Steam subscriptions'), expanded=True)
        self._subscribed_only = QCheckBox(self.tr('Subscribed mods only'))
        self._subscribed_only.setToolTip(self.tr('Show your Steam subscriptions for the selected game. Requires sign-in.'))
        self._subscribed_only.toggled.connect(self._subscriptions_filter_changed)
        subscriptions.addWidget(self._subscribed_only)
        local = self._filter_section(filters, self.tr('Content Options'))
        note = QLabel(self.tr('These filters apply to the current page.'))
        note.setWordWrap(True); note.setObjectName('Help'); local.addWidget(note)
        self._installed_filter = QComboBox()
        for label, value in [(self.tr('All mods'), 'all'), (self.tr('Installed'), 'installed'),
                             (self.tr('Not installed'), 'new')]:
            self._installed_filter.addItem(label, value)
        local.addWidget(self._installed_filter)
        self._hide_mature = QCheckBox(self.tr('Hide content flagged mature by Steam'))
        local.addWidget(self._hide_mature)
        self._installed_filter.currentIndexChanged.connect(self._local_changed)
        self._hide_mature.toggled.connect(self._local_changed)
        self._ranges = {}
        for key, label, presets in [
            ('size', self.tr('File Size'), [('1 MB', 1024**2), ('10 MB', 10*1024**2), ('100 MB', 100*1024**2), ('1 GB', 1024**3)]),
            ('subscriptions', self.tr('Subscribers'), [('100', 100), ('1,000', 1000), ('10,000', 10000), ('100,000', 100000)]),
            ('votes', self.tr('Votes'), [('10', 10), ('100', 100), ('1,000', 1000), ('10,000', 10000)])]:
            section = self._filter_section(filters, label)
            note = QLabel(self.tr('On this page')); note.setObjectName('Help'); section.addWidget(note)
            low = NexusBrowserView._range_combo(presets, self.tr('No minimum'))
            high = NexusBrowserView._range_combo(presets, self.tr('No maximum'))
            section.addWidget(low); section.addWidget(high)
            self._ranges[key] = low, high
            low.currentIndexChanged.connect(self._local_changed)
            high.currentIndexChanged.connect(self._local_changed)
        self._button(filters, self.tr('Clear advanced filters'), self._clear_filters)
        self._button(filters, self.tr('Open Steam Workshop'), lambda: _open_url(
            f'https://steamcommunity.com/app/{self._app_id}/workshop/'))
        filter_scroll.setWidget(body); panel.addWidget(filter_scroll, 1)
        self._filter_panel.setStyleSheet(NexusBrowserView._filter_qss(active_palette()))
        self._split.addWidget(self._filter_panel)
        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True); self._scroll.setFrameShape(QFrame.NoFrame)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        host = QWidget(); self._grid = QGridLayout(host)
        self._grid.setContentsMargins(16, 12, 16, 12); self._grid.setSpacing(12)
        self._grid.setAlignment(Qt.AlignTop)
        self._scroll.setWidget(host); self._scroll.installEventFilter(self)
        self._loading = LoadingOverlay(self._scroll)
        self._loading.hide_overlay()
        self._split.addWidget(self._scroll)
        self._split.setStretchFactor(0, 0); self._split.setStretchFactor(1, 1)
        self._split.setSizes([260, 1020])
        outer.addWidget(self._split, 1)
        self._selection_bar, row = self._bar()
        self._selection_label = QLabel(); row.addWidget(self._selection_label); row.add_stretch()
        self._button(row, self.tr('Clear selection'), self._clear_selection)
        self._selection_action = self._button(row, self.tr('Download selected'), self._download_selected)
        self._subscribe_selected = self._button(row, self.tr('Subscribe selected on Steam'),
            self._subscribe_selection)
        self._subscribe_selected.setVisible(self._subscribe is not None)
        outer.addWidget(self._selection_bar)
        footer, row = self._bar()
        self._modes = {self.tr('Text'): 'text', self.tr('Author ID'): 'author', self.tr('Item ID / URL'): 'item'}
        self._mode = SelectorButton(items=list(self._modes), current=self.tr('Text'), min_width=110,
                                    on_select=self._mode_changed)
        row.addWidget(self._mode)
        self._search = QLineEdit(); self._search.setFixedWidth(280)
        self._search.setPlaceholderText(self.tr('Search mods…')); self._search.setClearButtonEnabled(True)
        self._search.returnPressed.connect(self._reset); row.addWidget(self._search)
        self._search_mode = 'text'
        self._button(row, self.tr('Search'), self._reset)
        self._button(row, self.tr('Clear'), self._clear_search)
        row.add_stretch()
        self._perpage = SelectorButton(items=['20', '30', '40', '50', '100'], current=str(self._page_size),
                                       prefix=self.tr('Show: '), min_width=110, on_select=self._page_size_changed)
        row.addWidget(self._perpage)
        self._previous = self._button(row, self.tr('◂ Prev'), lambda: self._turn(-1))
        self._next_button = self._button(row, self.tr('Next ▸'), lambda: self._turn(1))
        row.addWidget(QLabel(self.tr('Page')))
        self._page_edit = QLineEdit('1'); self._page_edit.setFixedWidth(48)
        self._page_edit.setAlignment(Qt.AlignCenter)
        self._page_edit.setToolTip(self.tr('Jump to a page (Steam supports pages 1–1000).'))
        self._page_edit.returnPressed.connect(self._jump); row.addWidget(self._page_edit)
        self._status = QLabel(); self._status.setTextFormat(Qt.PlainText)
        self._status.setObjectName('Help'); self._status.setWordWrap(True)
        row.addWidget(self._status)
        outer.addWidget(footer)
        self._update_selection()
        self.refresh_install_labels()

    def _bar(self):
        bar = QWidget(); bar.setObjectName('HeaderBar')
        row = FlowLayout(bar, spacing=6); row.setContentsMargins(10, 6, 10, 6)
        policy = bar.sizePolicy(); policy.setHeightForWidth(True); bar.setSizePolicy(policy)
        return bar, row

    def _button(self, row, label, callback):
        button = QToolButton(); button.setObjectName('ActionButton'); button.setText(label)
        button.setCursor(Qt.PointingHandCursor); button.clicked.connect(callback); row.addWidget(button)
        return button

    def _filter_section(self, layout, title, expanded=False):
        from gui_qt.widgets.collapsible_section import CollapsibleSection
        section = CollapsibleSection(title)
        if expanded: section.expand()
        body = QVBoxLayout(section.body); body.setContentsMargins(0, 0, 0, 0); body.setSpacing(4)
        layout.addWidget(section)
        return body

    def _toggle_filters(self, checked):
        if not checked:
            self._filter_width = self._split.sizes()[0]
        self._filter_panel.setVisible(checked)
        if checked: self._split.setSizes([getattr(self, '_filter_width', 260), max(1, self._scroll.width())])

    def _set_section(self, name):
        self._section = name
        for key, button in self._sections.items(): button.setChecked(key == name)
        self._sort.setEnabled(name == 'Browse' and self._search_mode == 'text' and not self._subscribed_only.isChecked())
        self._reset()

    def _sort_changed(self, label):
        self._sort_value = self._sorts[label]; self._reset()

    def _time_changed(self, label):
        self._days = self._times[label]; self._reset()

    def _custom_time(self):
        from gui_qt.dialogs.date_picker import DatePickerOverlay
        today = QDate.currentDate()
        def chosen(date):
            if date is None: return
            self._days = max(1, date.daysTo(today))
            label = self.tr('Since {0}').format(date.toString('yyyy-MM-dd'))
            self._times[label] = self._days
            self._time.set_items(list(self._times), current=label)
            self._reset()
        DatePickerOverlay.show_over(self, self.tr('Uploaded since…'), today.addMonths(-3), today, chosen)

    def _mode_changed(self, label):
        self._search_mode = self._modes[label]
        self._search.clear()
        self._search.setPlaceholderText(self.tr('SteamID64 or steamcommunity.com/profiles/ URL') if self._search_mode == 'author'
                                        else self.tr('Workshop item ID or URL') if self._search_mode == 'item'
                                        else self.tr('Search mods…'))
        text_mode = self._search_mode == 'text'
        self._sort.setVisible(text_mode)
        self._time.setVisible(text_mode)
        for widget in (self._include, self._exclude): widget.setEnabled(text_mode)
        self._match_all.setEnabled(text_mode and not self._subscribed_only.isChecked())
        self._time.setEnabled(text_mode and not self._subscribed_only.isChecked())
        self._sort.setEnabled(text_mode and self._section == 'Browse' and not self._subscribed_only.isChecked())
        self._sections['Trending'].setEnabled(text_mode and not self._subscribed_only.isChecked())
        if not text_mode:
            self._section = 'Browse'
            for name, button in self._sections.items(): button.setChecked(name == 'Browse')

    def _subscriptions_filter_changed(self, checked):
        if checked:
            self._mode.set_current(self.tr('Text'))
            self._mode_changed(self.tr('Text'))
            self._section = 'Browse'
            for name, button in self._sections.items(): button.setChecked(name == 'Browse')
            self._clear_selection()
            self._match_all.setChecked(True)
        for widget in (self._mode, self._search, self._sort, self._time, self._sections['Trending']):
            widget.setEnabled(not checked)
        if not checked:
            self._mode_changed(self._mode.current())
        self._reset()

    def _page_size_changed(self, label):
        from .preferences import save_workshop_page_size
        self._page_size = int(label)
        try: save_workshop_page_size(self._page_size)
        except OSError as exc: self._log(f'[workshop] Could not save page size: {exc}')
        self._reset()

    def _clear_search(self):
        self._mode.set_current(self.tr('Text'))
        self._mode_changed(self.tr('Text'))
        self._reset()

    @staticmethod
    def _tags(text):
        return tuple(dict.fromkeys(tag.strip() for tag in text.split(',') if tag.strip()))

    def _reset(self, *_):
        self._page = 0
        self._query = dict(search=self._search.text().strip(), sort=3 if self._section == 'Trending' else self._sort_value,
                           tags=self._tags(self._include.text()), excluded_tags=self._tags(self._exclude.text()),
                           match_all=self._match_all.isChecked(),
                           since=int((datetime.now(timezone.utc) - timedelta(days=self._days)).timestamp()) if self._days else 0,
                           mode=self._search_mode)
        self._reload()

    def _reload(self):
        self._cancel.set(); self._action_cancel.set(); self._preparing = False
        self._stop_subscribed_listing()
        cancel = self._cancel = threading.Event()
        self._busy = True; self._update_selection()
        self._previous.setEnabled(False); self._next_button.setEnabled(False)
        self._scroll.widget().setEnabled(False); self._loading.show_over(self._scroll)
        self._status.setText(self.tr('Loading Steam Workshop…'))
        app_id, client, signal = self._app_id, self._client, self._results
        options = dict(self._query, page=self._page + 1, page_size=self._page_size, cancel=cancel)
        mode = options.pop('mode', 'text')
        subscribed = self._subscribed_only.isChecked()
        account = self._listing_account = saved_account() if subscribed else ''
        if subscribed:
            self._subscribed_checked = time.monotonic()
        if subscribed and account:
            client = self._subscribed_client = WorkshopClient(account=account)
        def worker(options=options):
            try:
                if subscribed:
                    if not account:
                        raise ValueError(self.tr('Sign in to Steam to view subscribed mods.'))
                    result = client.listing(app_id, subscribed=True, page=options['page'], page_size=options['page_size'],
                                            tags=options['tags'], excluded_tags=options['excluded_tags'], cancel=cancel)
                elif mode != 'text' and not options['search']:
                    raise ValueError(self.tr('Enter an author ID or Workshop item to search.'))
                elif mode == 'item' and options['search']:
                    from .download import parse_item_id
                    data = client.request(dict(op='details', appid=int(app_id), ids=[parse_item_id(options['search'])]), cancel)
                    entries = [parse_entry(raw, app_id) for raw in data['items']]
                    result = entries, len(entries), ''
                else:
                    if mode == 'author' and options['search']:
                        match = re.fullmatch(r'(?:https?://steamcommunity\.com/profiles/)?(\d{17})/?', options['search'])
                        if not match: raise ValueError(self.tr('Enter a SteamID64 or a numeric Steam profile URL.'))
                        options = dict(author=match[1], page=options['page'], page_size=options['page_size'], cancel=cancel)
                    result = client.listing(app_id, **options)
                safe_emit(signal, cancel, result, '')
            except DownloadCancelled: pass
            except Exception as exc: safe_emit(signal, cancel, None, str(exc))
            finally:
                if subscribed and account:
                    client.close()
        threading.Thread(target=worker, daemon=True, name='workshop-browse').start()

    def _received(self, token, result, error):
        if token is not self._cancel: return
        if self._subscribed_only.isChecked() and self._listing_account != saved_account():
            self._reset(); return
        self._busy = False; self._scroll.widget().setEnabled(True); self._loading.hide_overlay()
        self._error = error
        if error:
            self._entries, self._total = [], 0
            self._log(f'[workshop] {error}')
        else:
            self._entries, self._total, _ = result
            if self._subscribed_only.isChecked():
                self._subscriptions.update((entry.package_id, True) for entry in self._entries)
                if not self._entries and self._page > 0:
                    self._page = min(self._page - 1, max(0, (self._total - 1) // self._page_size))
                    self._reload(); return
            self._known_tags.update(tag for entry in self._entries for tag in entry.tags)
            self._rebuild_tags()
        self._previous.setEnabled(self._page > 0)
        self._next_button.setEnabled(self._page + 1 < min(1000, (self._total + self._page_size - 1) // self._page_size))
        self._page_edit.setText(str(self._page + 1))
        self._rebuild(); self._scroll.verticalScrollBar().setValue(0)

    def _rebuild_tags(self):
        for check in self._tag_checks.values():
            self._tag_layout.removeWidget(check); check.deleteLater()
        self._tag_checks = {}
        included = self._tags(self._include.text())
        for tag in sorted(self._known_tags | set(included), key=str.casefold)[:80]:
            check = QCheckBox(tag); check.setChecked(tag in included)
            check.toggled.connect(lambda on, t=tag: self._tag_toggled(t, on))
            self._tag_checks[tag] = check; self._tag_layout.addWidget(check)

    def _tag_toggled(self, tag, on):
        tags = list(self._tags(self._include.text()))
        if on and tag not in tags: tags.append(tag)
        if not on and tag in tags: tags.remove(tag)
        self._include.setText(', '.join(tags))
        if self._search_mode != 'text':
            self._mode.set_current(self.tr('Text')); self._mode_changed(self.tr('Text'))
        self._reset()

    def _clear_filters(self):
        self._include.clear(); self._exclude.clear(); self._match_all.setChecked(True)
        widgets = [self._subscribed_only, self._installed_filter, self._hide_mature] + [w for pair in self._ranges.values() for w in pair]
        for widget in widgets:
            widget.blockSignals(True)
            if isinstance(widget, QComboBox): widget.setCurrentIndex(0)
            else: widget.setChecked(False)
            widget.blockSignals(False)
        self._rebuild_tags(); self._subscriptions_filter_changed(False)

    def _local_changed(self, *_):
        self._action_cancel.set(); self._preparing = False
        self._rebuild()

    def _installed(self):
        return installed_items(self._game.get_effective_mod_staging_path(), self._app_id)

    def _visible_entries(self, installed):
        for entry in self._entries:
            state = self._installed_filter.currentData()
            if state == 'installed' and entry.package_id not in installed: continue
            if state == 'new' and entry.package_id in installed: continue
            if self._hide_mature.isChecked() and entry.mature: continue
            values = dict(size=entry.file_size, subscriptions=entry.subscriptions, votes=entry.votes)
            if any((lo.currentData() is not None and values[key] < lo.currentData()) or
                   (hi.currentData() is not None and values[key] > hi.currentData())
                   for key, (lo, hi) in self._ranges.items()): continue
            yield entry

    def _rebuild(self):
        self._thumbs.discard_pending()
        for card in self._cards:
            self._grid.removeWidget(card); card.deleteLater()
        self._cards = []
        installed = self._installed()
        for entry in self._visible_entries(installed):
            image_id = self._thumbnail_ids.setdefault(entry.preview, len(self._thumbnail_ids) + 1)
            iso = lambda stamp: datetime.fromtimestamp(stamp, timezone.utc).isoformat() if stamp else ''
            data = _Card(entry, image_id, entry.full_name, re.sub(r'\[/?[^\]]+\]', '', entry.description),
                         entry.preview, ', '.join(entry.tags[:3]), iso(entry.item.updated), iso(entry.created),
                         entry.file_size // 1024, entry.votes, entry.subscriptions)
            card = WorkshopModCard(data, lambda e: self._action(e.entry, 'view'),
                                lambda e: self._action(e.entry, 'install'), self._toggle_subscription,
                                on_context=self._context_menu,
                                on_select=self._select, selected=entry.package_id in self._selected,
                                is_installed=entry.package_id in installed, download_only=self._download_only())
            card._install_btn.setEnabled(supports_workshop_install(self._game))
            self._subscription_button(card._subscribe_button, entry)
            card.setToolTip(self.tr('Votes: {0} · Subscribers: {1}').format(entry.votes, entry.subscriptions))
            self._cards.append(card); self._thumbs.request(data.mod_id, data.picture_url)
        self._relayout(); self._update_selection()
        self._status.setText(getattr(self, '_error', '') or self.tr('{0} shown · {1} results').format(len(self._cards), self._total))
        self.refresh_subscriptions()

    def _subscription_button(self, button, entry):
        subscribed = self._subscriptions.get(entry.package_id)
        button.setText(self.tr('Unsubscribe') if subscribed else self.tr('Subscribe'))
        known = not saved_account() or subscribed is not None
        button.setEnabled(self._subscribe is not None and known)
        button.setToolTip((self.tr('Unsubscribe from this mod on Steam. Requirements are kept.') if subscribed else
                          self.tr('Subscribe on Steam without adding to Amethyst’s modlist.')) if known else
                         self._subscription_error or self.tr('Checking Steam subscription…'))

    def _refresh_subscription_buttons(self):
        for card in self._cards:
            self._subscription_button(card._subscribe_button, card.entry.entry)
        if self._detail is not None:
            self._subscription_button(self._detail._subscribe_button, self._detail._entry)

    def _toggle_subscription(self, entry):
        self.refresh_subscriptions()
        if saved_account() and entry.package_id not in self._subscriptions:
            self.refresh_subscriptions(force=True)
            return
        self._action(entry, 'unsubscribe' if self._subscriptions.get(entry.package_id) else 'subscribe')

    def _stop_subscription_check(self):
        self._subscription_cancel.set()
        if self._subscription_client is not None:
            self._subscription_client.close()
        self._subscription_client = None

    def _stop_subscribed_listing(self):
        if self._subscribed_client is not None:
            self._subscribed_client.close()
        self._subscribed_client = None

    def refresh_subscriptions(self, *, force=False):
        account = saved_account()
        try:
            configs = tuple(sorted((str(path), path.stat().st_mtime_ns)
                for path in (state_root() / 'account').rglob('account.config') if path.is_file()))
        except OSError:
            configs = ()
        scope = (self._app_id, account, configs)
        previous_scope = self._subscription_scope
        if force or scope != self._subscription_scope:
            self._stop_subscription_check()
            self._subscription_scope = scope
            self._subscriptions.clear()
            self._subscription_error = ''
            self._subscriptions_checked = 0
            self._refresh_subscription_buttons()
            if previous_scope is not None and scope != previous_scope and self._subscribed_only.isChecked():
                self._reset()
        if (account and self._subscribed_only.isChecked() and not self._busy and not self._preparing
                and self._detail is None and time.monotonic() - self._subscribed_checked >= 60):
            self._reload()
        if not account or self._subscription_client is not None or self._subscribe is None:
            return
        ids = {card.entry.entry.package_id for card in self._cards}
        if self._detail is not None:
            ids.add(self._detail._entry.package_id)
        if not ids or (time.monotonic() - self._subscriptions_checked < 60
                       and (ids <= self._subscriptions.keys() or self._subscription_error)):
            return
        cancel = self._subscription_cancel = threading.Event()
        client = self._subscription_client = WorkshopClient(account=account)
        self._subscriptions_checked = time.monotonic()
        app_id, signal = self._app_id, self._subscriptions_ready
        def worker():
            states, error = {}, ''
            try:
                ordered = sorted(ids)
                for start in range(0, len(ordered), 100):
                    batch = ordered[start:start + 100]
                    data = client.request(dict(op='subscriptions', appid=int(app_id), ids=batch), cancel)
                    values = {item['id']: item['subscribed'] for item in data['items']}
                    if set(values) != set(batch) or any(type(value) is not bool for value in values.values()):
                        raise ValueError('Steam did not return all subscription states.')
                    states.update(values)
            except DownloadCancelled:
                return
            except Exception as exc:
                error = str(exc)
            finally:
                client.close()
            if not cancel.is_set():
                safe_emit(signal, (cancel, scope), states, error)
        threading.Thread(target=worker, daemon=True, name='WorkshopSubscriptionStatus').start()

    def _subscriptions_received(self, token, states, error):
        cancel, scope = token
        if cancel is not self._subscription_cancel or cancel.is_set() or scope != self._subscription_scope:
            return
        self._subscription_client = None
        self._subscription_error = error
        if error:
            self._subscriptions.clear()
            self._log(f'[workshop] {error}')
        else:
            self._subscriptions.update(states)
        self._refresh_subscription_buttons()

    def _relayout(self):
        cols = max(1, (self._scroll.viewport().width() - 32 + 12) // (CARD_W + 12))
        while self._grid.count(): self._grid.takeAt(0)
        for col in range(self._grid.columnCount()): self._grid.setColumnStretch(col, 0)
        for index, card in enumerate(self._cards):
            self._grid.addWidget(card, index // cols, 1 + index % cols, Qt.AlignTop | Qt.AlignHCenter)
            card.show()
        self._grid.setColumnStretch(0, 1); self._grid.setColumnStretch(cols + 1, 1)
        self._cols = cols

    def _thumbnail(self, serial, pixmap):
        for card in self._cards:
            if card.entry.mod_id == serial: card.set_thumbnail(pixmap)

    def eventFilter(self, obj, event):
        if obj is self._scroll and event.type() == QEvent.Resize: self._relayout()
        return super().eventFilter(obj, event)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self._detail is not None:
            self._detail.setGeometry(self.rect())

    def _close_details(self):
        if self._detail is not None:
            self._detail.hide()
            self._detail.deleteLater()
            self._detail = None

    def _back(self):
        if self._detail is not None: self._close_details()
        else: self._turn(-1)

    def _turn(self, direction):
        if self._busy or self._detail is not None: return
        page = self._page + direction
        if page < 0 or page >= min(1000, max(1, (self._total + self._page_size - 1) // self._page_size)): return
        self._page = page; self._reload()

    def _jump(self):
        try: page = int(self._page_edit.text()) - 1
        except ValueError: page = self._page
        self._turn(page - self._page)
        self._page_edit.setText(str(self._page + 1))

    def _select(self, card, selected):
        if self._selection_context != self._context(): self._clear_selection()
        self._selection_context = self._context()
        if selected: self._selected[card.entry.package_id] = card.entry
        else: self._selected.pop(card.entry.package_id, None)
        self._update_selection()

    def _clear_selection(self):
        self._action_cancel.set(); self._preparing = False; self._selected.clear()
        for card in self._cards: card.set_selected(False)
        self._update_selection()

    def _update_selection(self):
        count = len(self._selected)
        self._selection_label.setText(self.tr('Preparing selected mods…') if self._preparing else self.tr('{0} mods selected').format(count))
        self._selection_bar.setVisible(count > 0 or self._preparing)
        enabled = not self._busy and not self._preparing and supports_workshop_install(self._game)
        self._selection_action.setEnabled(enabled and count > 0)
        self._install_all.setEnabled(enabled and bool(self._cards))
        self._subscribe_selected.setEnabled(not self._busy and not self._preparing and bool(self._selected))

    def _download_selected(self):
        if self._selection_context != self._context():
            self._clear_selection(); return
        self._prepare(list(self._selected.values()), 'batch')

    def _subscribe_selection(self):
        if self._selection_context != self._context():
            self._clear_selection(); return
        self._prepare(list(self._selected.values()), 'subscribe')

    def _install_visible(self):
        self._prepare([card.entry.entry for card in self._cards], 'batch')

    def _context_menu(self, card, position):
        entry = card.entry
        menu = QMenu(self)
        menu.addAction(self.tr('View details'), lambda: self._action(entry, 'view'))
        menu.addAction(self.tr('Open on Steam Workshop'), lambda: _open_url(entry.item.url))
        menu.addAction(self.tr('Copy Workshop URL'), lambda: QCoreApplication.instance().clipboard().setText(entry.item.url))
        menu.addAction(self.tr('Copy item ID'), lambda: QCoreApplication.instance().clipboard().setText(entry.package_id))
        action = menu.addAction(self.tr('Download') if self._download_only() else self.tr('Download and install'),
                                lambda: self._action(entry, 'install'))
        action.setEnabled(supports_workshop_install(self._game))
        if self._subscribe is not None:
            subscribed = self._subscriptions.get(entry.package_id)
            action = menu.addAction(self.tr('Unsubscribe on Steam') if subscribed else self.tr('Subscribe on Steam'),
                                   lambda: self._toggle_subscription(entry))
            action.setEnabled(not saved_account() or subscribed is not None)
        if entry.creator:
            menu.addAction(self.tr('Mods by this author'), lambda: self._browse_author(entry.creator))
            menu.addAction(self.tr('Open author profile'), lambda: _open_url(f'https://steamcommunity.com/profiles/{entry.creator}'))
        menu.exec(position)

    def _browse_author(self, author):
        if self._subscribed_only.isChecked(): self._subscribed_only.setChecked(False)
        self._mode.set_current(self.tr('Author ID')); self._mode_changed(self.tr('Author ID'))
        self._search.setText(author); self._reset()

    def _action(self, entry, operation):
        self._prepare([entry], operation)

    def _prepare(self, entries, operation):
        if not entries: return
        self._action_cancel.set(); cancel = self._action_cancel = threading.Event()
        context = self._context(); self._preparing = operation != 'view'; self._update_selection()
        self._status.setText(self.tr('Checking Workshop requirements…'))
        client, signal, app_id = self._client, self._action_ready, self._app_id
        def worker():
            try:
                if operation == 'view':
                    data = client.request(dict(op='details', appid=int(app_id), ids=[entries[0].package_id]), cancel)
                    result = parse_entry(data['items'][0], app_id)
                elif operation == 'unsubscribe': result = (entries, set())
                else: result = client.resolve_many(app_id, [entry.package_id for entry in entries], cancel)
                safe_emit(signal, (cancel, context, operation), result, entries, '')
            except DownloadCancelled: pass
            except Exception as exc: safe_emit(signal, (cancel, context, operation), None, entries, str(exc))
        threading.Thread(target=worker, daemon=True, name='workshop-details').start()

    def _action_received(self, token, result, requested, error):
        cancel, context, operation = token
        if cancel is not self._action_cancel or cancel.is_set(): return
        self._preparing = False; self._update_selection()
        if context != self._context():
            self._status.setText(self.tr('Game or profile changed. Select the mod again.')); return
        if error:
            self._status.setText(error); self._log(f'[workshop] {error}'); return
        self._status.clear()
        if operation == 'view':
            install = (lambda e: self._action(e, 'install')) if supports_workshop_install(self._game) else None
            self._close_details()
            self._detail = _Details(self, result, install, self._close_details,
                                    installed=result.package_id in self._installed())
            self.refresh_subscriptions()
            return
        packages, cycles = result
        if operation in {'subscribe', 'unsubscribe'}:
            if self._subscribe is not None:
                self._subscribe([entry.item for entry in packages], context, unsubscribe=operation == 'unsubscribe')
            return
        roots = {entry.package_id for entry in requested}
        installed = self._installed()
        from Utils.mods.modlist import read_modlist
        modlist = read_modlist(context[1] / 'modlist.txt') if context[1] else []
        enabled = {mod.name for mod in modlist if mod.enabled and not mod.is_separator}
        satisfied = {item_id for item_id, names in installed.items() if enabled.intersection(names)}
        plan = [entry for entry in packages if entry.package_id in roots or entry.package_id not in satisfied]
        download_only = self._download_only()
        required = {child for entry in packages for child in entry.children if child != entry.package_id}
        enable = {}
        if not download_only:
            for entry in plan:
                names = installed.get(entry.package_id)
                if names and entry.package_id in required and entry.package_id not in satisfied:
                    enable[entry.package_id] = next((mod.name for mod in modlist
                        if not mod.is_separator and mod.name in names), min(names))
        reuse = {item_id: name for item_id, name in enable.items() if item_id not in roots}
        notice = self.tr("Some requirements depend on each other. Check the game's load order after installation.") if cycles else ''
        def chosen(items):
            if items is None: return
            if cancel is not self._action_cancel or cancel.is_set() or context != self._context():
                self._status.setText(self.tr('Game, profile or page changed. Select the mods again.')); return
            if download_only != self._download_only():
                self._prepare(requested, operation); return
            selected = {entry.package_id: enable[entry.package_id] for entry in items if entry.package_id in enable}
            self._install([entry.item for entry in items if entry.package_id not in reuse], context,
                          enable_requirements=selected)
        if operation == 'batch':
            from gui_qt.dialogs.confirm import ConfirmOverlay
            from Utils.downloads.core import fmt_size
            ConfirmOverlay.show_over(self, self.tr('Download selected Workshop mods?'),
                self.tr('{0} mods including requirements · {1} to download. Enabled requirements are skipped.').format(
                    len(plan), fmt_size(sum(entry.file_size for entry in plan if entry.package_id not in reuse)))
                + ('\n' + self.tr('Installed requirements marked below will be enabled in this profile.') if enable else '')
                + ('\n' + notice if notice else ''),
                lambda yes: chosen(plan) if yes else None, confirm_label=self.tr('Download'),
                cancel_label=self.tr('Cancel'), danger=False, list_items=[
                    self.tr('{0} — enable installed mod').format(entry.full_name)
                    if entry.package_id in reuse else
                    self.tr('{0} — reinstall and enable').format(entry.full_name)
                    if entry.package_id in enable else entry.full_name for entry in plan])
        else:
            root = next(entry for entry in packages if entry.package_id in roots)
            deps = [entry for entry in plan if entry.package_id not in roots]
            if deps:
                from .dependencies import WorkshopDepsOverlay
                WorkshopDepsOverlay.show_over(
                    self, root=root, dependencies=deps, on_done=chosen,
                    notice=notice, installed=reuse)
            else: chosen([root])

    @staticmethod
    def _download_only():
        from Utils.ui.config import load_download_only
        return bool(load_download_only())

    def refresh_install_labels(self):
        for card in self._cards: card.set_download_only(self._download_only())
        self._install_all.setText(self.tr('Download all') if self._download_only() else self.tr('Install all'))
        if self._detail is not None: self._detail.refresh_install_labels()

    def refresh_date_format(self):
        for card in self._cards: card.refresh_date_format()

    def refresh_installed(self):
        if self._selection_context != self._context(): self._clear_selection()
        if self._subscribed_only.isChecked():
            self._reload(); return
        self._rebuild()
        if self._detail is not None:
            self._detail._installed = self._detail._entry.package_id in self._installed()
            self._detail.refresh_install_labels()

    def set_game(self, game):
        self._close_details()
        self._action_cancel.set(); self._clear_selection()
        self._game = game
        app_id = workshop_app_id(game)
        if app_id == self._app_id:
            self.refresh_installed(); return
        self._app_id = app_id; self._known_tags.clear()
        self._mode.set_current(self.tr('Text')); self._mode_changed(self.tr('Text'))
        self._search.clear(); self._include.clear(); self._exclude.clear()
        self._rebuild_tags(); self._clear_filters()
