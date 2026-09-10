from __future__ import annotations

import copy
import configparser
import queue
import threading
import time
import uuid
import weakref
import zipfile
from pathlib import Path

from PySide6.QtCore import Qt, Signal, QTimer, QEvent
from PySide6.QtGui import QIntValidator, QTextDocument
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QLineEdit,
    QComboBox, QListWidget, QListWidgetItem, QStackedWidget,
    QFormLayout, QPlainTextEdit, QTableWidget, QTableWidgetItem, QHeaderView, QScrollArea,
    QGridLayout, QFrame, QToolButton, QMenu, QBoxLayout, QSizePolicy, QProgressBar,
)

from gui_qt.safe_emit import safe_emit
from gui_qt.theme_qt import active_palette, _c
from gui_qt.selector_button import SelectorButton
from gui_qt.tri_state_checkbox import TriStateCheckBox
from gui_qt.loading_overlay import LoadingOverlay
from gui_qt.mouse_navigation import MouseNavigationFilter
from gui_qt.wabbajack_card import WabbajackCard, CARD_W, IMG_W, IMG_H
from gui_qt.wabbajack_setup import RequirementsSummary, AcquisitionSummary, CappedComboBox
from gui_qt.icons import icon
from Utils.collections.manifest import fmt_size
from Utils.downloads.install import InstallCallbacks, InstallControl
from Utils.wabbajack.diagnostics import emit, emit_exception


PAGE_SIZE = 20
HERO_W, HERO_H = 248, 140
DESC_MIN_H, DESC_MAX_H = 46, 260
_VIEWS = weakref.WeakSet()


def pause_for_shutdown():
    pending = False
    for view in list(_VIEWS):
        view.prepare_shutdown()
        pending |= view._shutdown_thread.is_alive()
    return pending


SORTS = [("Featured first", "featured"), ("Name (A–Z)", "name"), ("Name (Z–A)", "name_desc"),
         ("Largest install", "size_desc"), ("Smallest install", "size")]


class ElidedPathLabel(QLabel):
    """Shows a filesystem path on one line, eliding the middle and keeping the tail."""

    def __init__(self, placeholder="", parent=None):
        super().__init__(parent)
        self._path = ""
        self._placeholder = placeholder
        self.setTextFormat(Qt.PlainText)
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.setTextInteractionFlags(Qt.TextSelectableByMouse)

    def set_path(self, path):
        self._path = str(path or "")
        self.setToolTip(self._path)
        self._render()

    def _render(self):
        palette = active_palette()
        text = self._path or self._placeholder
        tone = "TEXT_MAIN" if self._path else "TEXT_FAINT"
        self.setStyleSheet(f"color:{_c(palette, tone)};")
        width = max(60, self.width())
        self.setText(self.fontMetrics().elidedText(text, Qt.ElideMiddle, width))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._render()


class WabbajackView(QWidget):
    _result = Signal(str, object, str)
    _progress = Signal(str, object)
    _manual = Signal(object)
    _conflicts = Signal(object)
    _path_picked = Signal(str, int, object)
    installed = Signal(object, object)
    running_changed = Signal(bool)
    gallery_changed = Signal(object)

    def __init__(self, game, get_api, log_fn=None, can_install=None, parent=None):
        super().__init__(parent)
        self._game = game
        self._get_api = get_api
        self._log = log_fn or (lambda _: None)
        self._can_install = can_install or (lambda: True)
        self._entries = []
        self._installed = []
        self._entry = None
        self._info = None
        self._package = None
        self._loading_package = False
        self._check_after_package = False
        self._package_url = ""
        self._manual_package = False
        self._request = None
        self._report = None
        self._preflight_stop = threading.Event()
        self._page = 0
        self._busy = False
        self._checking = False
        self._installing_mpi = False
        self._installing_texture = False
        self._worker_stop = threading.Event()
        self._package_stop = None
        self._workers = set()
        self._workers_lock = threading.Lock()
        self._shutting_down = False
        self._shutdown_thread = None
        _VIEWS.add(self)
        self._control = InstallControl()
        self._answers = queue.Queue()
        self._overlay = None
        self._manual_overlay = None
        self._manual_row = None
        self._manual_auto_open = False
        self._tokens = {}
        self._thumb_ids = {}
        self._thumb_urls = {}
        self._cards = []
        self._columns = 0
        self._sort = "featured"
        self._saved_filters = {
            key: self._load_filter(key) for key in (
                "featured_only", "installed_only", "show_adult",
                "hide_unavailable",
            )
        }
        self._thumb_sequence = 0
        self._result.connect(self._received)
        self._progress.connect(self._on_progress)
        self._manual.connect(self._on_manual)
        self._conflicts.connect(self._review_conflicts)
        self._path_picked.connect(self._on_path_picked)
        self._build()
        from gui_qt.nexus_mod_card import ThumbnailLoader
        self._thumbs = ThumbnailLoader(self, crop_w=IMG_W, crop_h=IMG_H)
        self._thumbs.loaded.connect(self._thumbnail)
        self._refresh_installed()
        self._diag("ui.opened", game=getattr(game, "name", None),
                   game_id=getattr(game, "game_id", None))
        self._load_gallery()

    def _diagnostic_log(self, message):
        safe_emit(self._progress, "log", (message,))

    def _diag(self, event, **fields):
        emit(self._diagnostic_log, event, **fields)

    def _button(self, text, callback, layout):
        button = QPushButton(self.tr(text), self)
        button.setObjectName("FormButton")
        button.clicked.connect(callback)
        layout.addWidget(button)
        return button

    def _tool_button(self, text, callback, layout):
        button = QToolButton(self)
        button.setText(self.tr(text))
        button.setObjectName("ActionButton")
        button.setCursor(Qt.PointingHandCursor)
        button.clicked.connect(callback)
        layout.addWidget(button)
        return button

    def _build(self):
        palette = active_palette()
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        toolbar = QWidget(self)
        toolbar.setObjectName("HeaderBar")
        tools = QHBoxLayout(toolbar)
        tools.setContentsMargins(10, 6, 10, 6)
        title = QLabel(self.tr("Wabbajack modlists"), toolbar)
        title.setStyleSheet(f"color:{_c(palette, 'TEXT_MAIN')}; font-weight:600;")
        tools.addWidget(title)
        tools.addStretch()
        self._open_button = self._tool_button("Open .wabbajack…", self._open_file, tools)
        self._url_button = self._tool_button("Open URL…", self._open_url, tools)
        self._refresh_button = self._tool_button("Refresh", lambda: self._load_gallery(True), tools)
        outer.addWidget(toolbar)
        self._stack = QStackedWidget(self)
        outer.addWidget(self._stack, 1)
        self._status = QLabel(self)
        self._status.setWordWrap(True)
        self._status.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self._status.setMargin(6)
        self._status.setTextFormat(Qt.PlainText)
        self._status.setStyleSheet(f"color:{_c(palette, 'TEXT_DIM')};")
        outer.addWidget(self._status)

        browser = QWidget(self)
        layout = QVBoxLayout(browser)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        filters = QWidget(browser)
        filters.setObjectName("HeaderBar")
        filter_layout = QVBoxLayout(filters)
        filter_layout.setContentsMargins(10, 6, 10, 6)
        choices = QHBoxLayout()
        self._current_game = QLabel(self._game.name if self._game else self.tr("No game selected"), filters)
        self._current_game.setTextFormat(Qt.PlainText)
        self._current_game.setWordWrap(True)
        self._current_game.setStyleSheet(f"color:{_c(palette, 'TEXT_DIM')};")
        choices.addWidget(self._current_game, 1)
        self._tag = CappedComboBox(self)
        self._tag.addItem(self.tr("All tags"), "")
        self._tag.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self._tag.setMinimumContentsLength(12)
        choices.addWidget(self._tag)
        self._sort_sel = SelectorButton(items=[self.tr(label) for label, _ in SORTS],
            current=self.tr(SORTS[0][0]), prefix=self.tr("Sort: "), min_width=170,
            on_select=self._sort_changed, scroll_after=15)
        choices.addWidget(self._sort_sel)
        filter_layout.addLayout(choices)
        switches = QHBoxLayout()
        self._featured = TriStateCheckBox(self.tr("Featured only"), self, two_state=True)
        self._only_installed = TriStateCheckBox(self.tr("Installed"), self, two_state=True)
        self._adult = TriStateCheckBox(self.tr("Show adult"), self, two_state=True)
        self._hide_unavailable = TriStateCheckBox(self.tr("Hide unavailable"), self, two_state=True)
        filter_switches = (
            (self._featured, "featured_only"),
            (self._only_installed, "installed_only"),
            (self._adult, "show_adult"),
            (self._hide_unavailable, "hide_unavailable"),
        )
        for checkbox, key in filter_switches:
            checkbox.set_state(int(self._saved_filters[key]))
            switches.addWidget(checkbox)
        switches.addStretch()
        self._results_label = QLabel(self)
        self._results_label.setStyleSheet(f"color:{_c(palette, 'TEXT_DIM')};")
        switches.addWidget(self._results_label)
        filter_layout.addLayout(switches)
        layout.addWidget(filters)

        self._scroll = QScrollArea(browser)
        self._scroll.setFrameShape(QFrame.NoFrame)
        self._scroll.setWidgetResizable(True)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        host = QWidget()
        self._grid = QGridLayout(host)
        self._grid.setContentsMargins(16, 12, 16, 12)
        self._grid.setSpacing(12)
        self._grid.setAlignment(Qt.AlignTop | Qt.AlignHCenter)
        self._scroll.setWidget(host)
        self._scroll.viewport().installEventFilter(self)
        self._empty = QLabel(self.tr("Loading modlists…"), host)
        self._empty.setAlignment(Qt.AlignCenter)
        self._empty.setWordWrap(True)
        self._empty.setMinimumHeight(220)
        self._empty.setStyleSheet(f"color:{_c(palette, 'TEXT_DIM')}; font-size:14px;")
        self._grid.addWidget(self._empty, 0, 0)
        self._loading_overlay = LoadingOverlay(self._scroll)
        layout.addWidget(self._scroll, 1)

        footer = QWidget(browser)
        footer.setObjectName("HeaderBar")
        paging = QHBoxLayout(footer)
        paging.setContentsMargins(10, 6, 10, 6)
        self._search = QLineEdit(self)
        self._search.setPlaceholderText(self.tr("Search titles, authors, or tags…"))
        self._search.setClearButtonEnabled(True)
        self._search.setMinimumWidth(150)
        self._search.setMaximumWidth(320)
        paging.addWidget(self._search, 1)
        self._tool_button("Search", self._filter_changed, paging)
        paging.addStretch()
        self._prev_button = self._tool_button("◂ Prev", lambda: self._turn_page(-1), paging)
        self._next_button = self._tool_button("Next ▸", lambda: self._turn_page(1), paging)
        paging.addWidget(QLabel(self.tr("Page"), footer))
        self._page_edit = QLineEdit("1", footer)
        self._page_edit.setAlignment(Qt.AlignCenter)
        self._page_edit.setFixedWidth(42)
        self._page_edit.setValidator(QIntValidator(1, 1_000_000, self._page_edit))
        self._page_edit.returnPressed.connect(self._jump_page)
        paging.addWidget(self._page_edit)
        self._page_label = QLabel(self)
        paging.addWidget(self._page_label)
        layout.addWidget(footer)
        self._stack.addWidget(browser)
        self._tag.currentIndexChanged.connect(self._filter_changed)
        for widget, key in filter_switches:
            widget.stateChanged.connect(
                lambda state, key=key: self._filter_toggled(key, state))
        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(250)
        self._search_timer.timeout.connect(self._filter_changed)
        self._search.textChanged.connect(lambda: self._search_timer.start())
        self._search.returnPressed.connect(self._filter_changed)
        self._mouse_navigation = MouseNavigationFilter(self, self._back, self._forward)

        detail_page = QWidget(self)
        page_layout = QVBoxLayout(detail_page)
        page_layout.setContentsMargins(0, 0, 0, 0)
        page_layout.setSpacing(0)
        detail = QWidget(detail_page)
        layout = QVBoxLayout(detail)
        layout.setContentsMargins(16, 12, 16, 12)
        layout.setSpacing(12)
        bar = QHBoxLayout()
        self._tool_button("← Back to browser", self._back_to_browser, bar)
        bar.addStretch()
        self._target_game = QLabel(self._game.name if self._game else self.tr("Select a game in the main toolbar"), detail)
        self._target_game.setTextFormat(Qt.PlainText)
        self._target_game.setWordWrap(True)
        self._target_game.setStyleSheet(f"color:{_c(palette, 'TEXT_DIM')};")
        bar.addWidget(self._target_game)
        layout.addLayout(bar)

        overview, description_layout = self._panel("")
        layout.addWidget(overview)
        summary = QHBoxLayout()
        summary.setSpacing(18)
        description_layout.addLayout(summary)
        self._detail_image = QLabel(overview)
        self._detail_image.setFixedSize(HERO_W, HERO_H)
        self._detail_image.setAlignment(Qt.AlignCenter)
        self._detail_image.setStyleSheet(f"background:{_c(palette, 'BG_DEEP')}; border-radius:6px;")
        summary.addWidget(self._detail_image, 0, Qt.AlignTop)
        summary_text = QVBoxLayout()
        summary_text.setSpacing(5)
        summary.addLayout(summary_text, 1)
        heading = QHBoxLayout()
        self._title = QLabel(overview)
        self._title.setTextFormat(Qt.PlainText)
        self._title.setWordWrap(True)
        self._title.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self._title.setStyleSheet(f"color:{_c(palette, 'TEXT_MAIN')}; font-size:22px; font-weight:700;")
        heading.addWidget(self._title, 1)
        self._setup_state = QLabel(self.tr("Not checked"), overview)
        self._setup_state.setAlignment(Qt.AlignCenter)
        self._setup_state.setSizePolicy(QSizePolicy.Minimum, QSizePolicy.Fixed)
        heading.addWidget(self._setup_state)
        summary_text.addLayout(heading)
        self._detail_author = QLabel(self)
        self._detail_author.setTextFormat(Qt.PlainText)
        self._detail_author.setWordWrap(True)
        self._detail_author.setStyleSheet(f"color:{_c(palette, 'TEXT_DIM')};")
        summary_text.addWidget(self._detail_author)
        self._detail_tags = QLabel(self)
        self._detail_tags.setTextFormat(Qt.PlainText)
        self._detail_tags.setWordWrap(True)
        self._detail_tags.setStyleSheet(f"color:{_c(palette, 'ACCENT')};")
        summary_text.addWidget(self._detail_tags)
        self._description = QPlainTextEdit(self)
        self._description.setReadOnly(True)
        self._description.setFrameShape(QFrame.NoFrame)
        self._description.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._description.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        self._description.setFixedHeight(DESC_MIN_H)
        self._description.setStyleSheet(f"background:transparent; color:{_c(palette, 'TEXT_DIM')};")
        self._description.viewport().installEventFilter(self)
        summary_text.addWidget(self._description)
        figures = QHBoxLayout()
        figures.setSpacing(22)
        self._figures = {}
        for key, title in (("download", "Download"), ("install", "On disk"), ("free", "Free space")):
            cell = QVBoxLayout()
            cell.setSpacing(0)
            caption = QLabel(self.tr(title), overview)
            caption.setStyleSheet(f"color:{_c(palette, 'TEXT_FAINT')}; font-size:10px; font-weight:600;")
            cell.addWidget(caption)
            value = QLabel("—", overview)
            value.setTextFormat(Qt.PlainText)
            value.setStyleSheet(f"color:{_c(palette, 'TEXT_MAIN')}; font-size:16px; font-weight:600;")
            cell.addWidget(value)
            figures.addLayout(cell)
            self._figures[key] = (caption, value)
        self._detail_sizes = QLabel(self)
        self._detail_sizes.setWordWrap(True)
        self._detail_sizes.setStyleSheet(f"color:{_c(palette, 'TEXT_DIM')};")
        figures.addSpacing(4)
        figures.addWidget(self._detail_sizes, 1, Qt.AlignBottom)
        links = QHBoxLayout()
        for text, kind in (("Author instructions", "readme"), ("Community", "community")):
            button = QToolButton(overview)
            button.setText(self.tr(text))
            button.setCursor(Qt.PointingHandCursor)
            button.setStyleSheet(f"color:{_c(palette, 'ACCENT')}; padding:2px 0;")
            button.clicked.connect(lambda checked=False, kind=kind: self._open_link(kind))
            links.addWidget(button)
        links.setSpacing(16)
        figures.addLayout(links)
        summary_text.addSpacing(3)
        summary_text.addLayout(figures)

        locations, location_layout = self._panel(self.tr("Files and locations"))
        layout.addWidget(locations)
        form = QFormLayout()
        form.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
        form.setRowWrapPolicy(QFormLayout.WrapLongRows)
        source = QHBoxLayout()
        self._package_path = QLabel(self.tr("The package is loaded when you check requirements."), locations)
        self._package_path.setTextFormat(Qt.PlainText)
        self._package_path.setWordWrap(True)
        self._package_path.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        source.addWidget(self._package_path, 1)
        self._prepare_button = self._button("Reload", self._prepare_entry, source)
        self._prepare_button.hide()
        self._choose_package_button = self._button("Choose file…", self._open_file, source)
        package = QVBoxLayout()
        package.setSpacing(5)
        package.addLayout(source)
        progress = QHBoxLayout()
        progress.setSpacing(8)
        self._package_progress = QProgressBar(locations)
        self._package_progress.setRange(0, 1000)
        self._package_progress.setTextVisible(False)
        self._package_progress.setFixedHeight(8)
        progress.addWidget(self._package_progress, 1)
        self._package_progress_text = QLabel(locations)
        self._package_progress_text.setTextFormat(Qt.PlainText)
        self._package_progress_text.setStyleSheet(f"color:{_c(palette, 'TEXT_DIM')}; font-size:11px;")
        progress.addWidget(self._package_progress_text)
        self._cancel_package_button = self._button("Cancel download", self._cancel_package_download, progress)
        self._cancel_package_button.hide()
        package.addLayout(progress)
        self._package_progress.hide()
        self._package_progress_text.hide()
        form.addRow(self.tr("Modlist package"), package)
        self._downloads = QLineEdit(self)
        self._downloads.setPlaceholderText(self.tr("Reuse an existing download folder"))
        self._downloads.hide()
        self._directory = QLineEdit(self)
        self._directory.setToolTip(self.tr("This installation's managed directory inside the current game's .wabbajack folder."))
        self._directory.hide()
        form.addRow(self.tr("Downloads"),
                    self._path_row(locations, self._downloads, self._choose_downloads,
                                   self.tr("Reuse an existing download folder")))
        form.addRow(self.tr("Installation"),
                    self._path_row(locations, self._directory, self._choose_directory,
                                   self.tr("Chosen automatically for this game")))
        location_layout.addLayout(form)
        self._detail_columns = QBoxLayout(QBoxLayout.LeftToRight)
        self._detail_columns.setSpacing(12)
        layout.addLayout(self._detail_columns)
        setup, setup_layout = self._panel(self.tr("Profiles and options"))
        self._detail_columns.addWidget(setup, 1, Qt.AlignTop)
        form = QFormLayout()
        self._setup_form = form
        form.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
        form.setRowWrapPolicy(QFormLayout.WrapLongRows)
        self._mode = CappedComboBox(self)
        for label, mode in (("New installation", "install"), ("Resume", "resume"), ("Repair", "repair"), ("Update", "update")):
            self._mode.addItem(self.tr(label), mode)
        self._mode.installEventFilter(self)
        form.addRow(self.tr("Operation"), self._mode)
        self._profiles = QListWidget(self)
        self._profiles.setMaximumHeight(110)
        self._profiles.setMinimumHeight(24)
        self._profiles.setSelectionMode(QListWidget.NoSelection)
        self._profiles.setStyleSheet("background:transparent; border:none;")
        form.addRow(self.tr("Profiles"), self._profiles)
        self._adjustments = QListWidget(self)
        self._adjustments.setMaximumHeight(100)
        self._adjustments.setMinimumHeight(24)
        self._adjustments.setSelectionMode(QListWidget.NoSelection)
        self._adjustments.setStyleSheet("background:transparent; border:none;")
        form.addRow(self.tr("Linux adjustments"), self._adjustments)
        form.setRowVisible(self._profiles, False)
        form.setRowVisible(self._adjustments, False)
        setup_layout.addLayout(form)
        from gui_qt.wabbajack_tasks import SetupOptions
        self._task_options = SetupOptions(
            setup, get_api=self._get_api, log_fn=self._diagnostic_log,
            can_install_tool=self._can_install_mpi)
        self._task_options.changed.connect(self._options_changed)
        self._task_options.tool_running_changed.connect(self._mpi_running_changed)
        self._task_options.tool_ready.connect(self._mpi_ready)
        setup_layout.addWidget(self._task_options)
        self._setup_hint = QLabel(self.tr("Check requirements to load the authored profiles and prepare the download plan."), setup)
        self._setup_hint.setWordWrap(True)
        setup_layout.addWidget(self._setup_hint)
        note = QLabel(self.tr("Editing shared mod files affects every profile. INIs, enabled mods and load order stay separate."), self)
        note.setWordWrap(True)
        note.setStyleSheet(f"color:{_c(palette, 'TEXT_DIM')};")
        setup_layout.addWidget(note)
        setup_layout.addStretch()
        acquisition, acquisition_layout = self._panel(self.tr("Download plan"))
        self._acquisition_summary = AcquisitionSummary(acquisition)
        acquisition_layout.addWidget(self._acquisition_summary)
        acquisition_layout.addStretch()
        self._detail_columns.addWidget(acquisition, 1, Qt.AlignTop)
        requirements, checks_layout = self._panel(self.tr("Requirements"))
        self._checks = RequirementsSummary(requirements)
        checks_layout.addWidget(self._checks)
        self._texture_button = self._button("Prepare / repair texture tool", self._install_texconv, checks_layout)
        self._texture_button.hide()
        layout.addWidget(requirements)
        layout.addStretch()
        self._detail_scroll = QScrollArea(detail_page)
        self._detail_scroll.setWidgetResizable(True)
        self._detail_scroll.setFrameShape(QFrame.NoFrame)
        self._detail_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._detail_scroll.setWidget(detail)
        self._detail_scroll.viewport().installEventFilter(self)
        page_layout.addWidget(self._detail_scroll, 1)
        footer = QFrame(detail_page)
        footer.setObjectName("HeaderBar")
        actions = QHBoxLayout(footer)
        actions.setContentsMargins(16, 10, 16, 10)
        self._steps = []
        rail = QHBoxLayout()
        rail.setSpacing(7)
        for index, title in enumerate((self.tr("Check"), self.tr("Review"), self.tr("Install"))):
            if index:
                dash = QFrame(footer)
                dash.setFixedSize(14, 1)
                dash.setStyleSheet(f"background:{_c(palette, 'BORDER')};")
                rail.addWidget(dash, 0, Qt.AlignVCenter)
            step = QLabel(f"{index + 1}  {title}", footer)
            step.setTextFormat(Qt.PlainText)
            rail.addWidget(step, 0, Qt.AlignVCenter)
            self._steps.append(step)
        actions.addLayout(rail)
        actions.addSpacing(16)
        self._footer_hint = QLabel(self.tr("Check requirements and review the download plan."), footer)
        self._footer_hint.setWordWrap(True)
        self._footer_hint.setStyleSheet(f"color:{_c(palette, 'TEXT_DIM')};")
        actions.addWidget(self._footer_hint, 1)
        self._start_button = self._button("Check requirements", self._primary_action, actions)
        self._start_button.setObjectName("PrimaryButton")
        self._start_button.setMinimumSize(170, 40)
        self._start_button.setStyleSheet("#PrimaryButton { min-height:40px; }")
        self._start_button.setCursor(Qt.PointingHandCursor)
        self._start_button.setEnabled(False)
        page_layout.addWidget(footer)
        self._stack.addWidget(detail_page)
        for widget in (self._directory, self._downloads):
            widget.textChanged.connect(self._invalidate)
        self._mode.currentIndexChanged.connect(self._options_changed)

        review = QWidget(self)
        layout = QVBoxLayout(review)
        layout.addWidget(QLabel(self.tr("Review changes before updating shared files and profiles."), self))
        self._conflict_table = QTableWidget(0, 3, self)
        self._conflict_table.setHorizontalHeaderLabels([self.tr("File"), self.tr("Change"), self.tr("Resolution")])
        self._conflict_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self._conflict_table.currentCellChanged.connect(self._show_conflict)
        layout.addWidget(self._conflict_table)
        self._conflict_details = QPlainTextEdit(self)
        self._conflict_details.setReadOnly(True)
        self._conflict_details.setMaximumHeight(220)
        layout.addWidget(self._conflict_details)
        actions = QHBoxLayout()
        self._button("Keep all mine", lambda: self._set_choices(0), actions)
        self._button("Use all author versions", lambda: self._set_choices(1), actions)
        self._button("Apply reviewed choices", self._accept_conflicts, actions)
        self._button("Pause update", self._pause, actions)
        layout.addLayout(actions)
        self._stack.addWidget(review)

    def _worker(self, kind, work):
        if self._shutting_down:
            return
        token = self._tokens.get(kind, 0) + 1
        self._tokens[kind] = token
        def run():
            started = time.monotonic()
            self._diag("ui.worker.started", kind=kind, token=token)
            try:
                result = work()
                self._diag("ui.worker.completed", kind=kind, token=token,
                           result_type=type(result).__name__,
                           elapsed_seconds=round(time.monotonic() - started, 3))
                if not self._shutting_down:
                    safe_emit(self._result, kind, (token, result), "")
            except Exception as exc:
                emit_exception(self._diagnostic_log, "ui.worker.failed", exc,
                               kind=kind, token=token,
                               elapsed_seconds=round(time.monotonic() - started, 3))
                if not self._shutting_down:
                    safe_emit(self._result, kind, (token, None), str(exc))
            finally:
                with self._workers_lock:
                    self._workers.discard(threading.current_thread())
        worker = threading.Thread(target=run, daemon=True, name=f"wabbajack-{kind}")
        with self._workers_lock:
            self._workers.add(worker)
            worker.start()

    def prepare_shutdown(self):
        if self._shutting_down:
            return
        self._shutting_down = True
        self._diag("ui.shutdown.started", workers=len(self._workers),
                   installing=self._busy, checking=self._checking)
        self._worker_stop.set()
        self._task_options.stop_tool()
        self._stop_package_download()
        self._preflight_stop.set()
        self._pause()
        with self._workers_lock:
            workers = list(self._workers)
        def finish():
            for worker in workers:
                worker.join()
            from Utils.wabbajack.textures import shutdown_texture_tools
            shutdown_texture_tools()
        self._shutdown_thread = threading.Thread(target=finish, name="wabbajack-shutdown", daemon=True)
        self._shutdown_thread.start()

    def _load_gallery(self, refresh=False):
        if self._busy:
            return
        self._status.setText(self.tr("Loading modlists…"))
        self._loading_overlay.show_over()
        self._refresh_button.setEnabled(False)
        self._diag("ui.gallery.requested", refresh=refresh)
        from Utils.wabbajack.gallery import load_gallery
        self._worker("gallery", lambda: load_gallery(
            refresh=refresh, stop=self._worker_stop, log=self._diagnostic_log))

    def _refresh_installed(self):
        from Utils.wabbajack.store import installations
        self._installed = installations(Path(self._game.get_profile_root()),
                                        self._diagnostic_log) if self._game else []

    def _panel(self, title):
        panel = QFrame(self)
        panel.setObjectName("WabbajackPanel")
        palette = active_palette()
        panel.setStyleSheet(f"#WabbajackPanel {{ background:{_c(palette, 'BG_PANEL')}; border:1px solid {_c(palette, 'BORDER')}; border-radius:6px; }}")
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(12, 10, 12, 12)
        if title:
            label = QLabel(title, panel)
            label.setStyleSheet(f"color:{_c(palette, 'TEXT_MAIN')}; font-weight:600;")
            layout.addWidget(label)
        return panel, layout

    def _add_choice(self, choices, label, value, checked):
        item = QListWidgetItem(choices)
        item.setData(Qt.UserRole, value)
        checkbox = TriStateCheckBox(label, choices, two_state=True)
        checkbox.setToolTip(label)
        checkbox.setFocusPolicy(Qt.StrongFocus)
        checkbox.set_state(int(checked))
        checkbox.stateChanged.connect(self._options_changed)
        size = checkbox.sizeHint()
        size.setWidth(0)
        item.setSizeHint(size)
        choices.setItemWidget(item, checkbox)

    @staticmethod
    def _selected_choices(choices):
        return [item.data(Qt.UserRole) for row in range(choices.count())
                if (item := choices.item(row)) is not None and choices.itemWidget(item).state()]

    def _filter_changed(self, *_):
        self._search_timer.stop()
        self._page = 0
        self._render()
        self._scroll.verticalScrollBar().setValue(0)

    @staticmethod
    def _load_filter(key):
        try:
            from Utils.ui.config import load_wabbajack_filter
            return bool(load_wabbajack_filter(key))
        except Exception:
            return False

    def _filter_toggled(self, key, state):
        try:
            from Utils.ui.config import save_wabbajack_filter
            save_wabbajack_filter(key, bool(state))
        except Exception:
            pass
        self._filter_changed()

    def _sort_changed(self, label):
        self._sort = next((key for title, key in SORTS if self.tr(title) == label), "featured")
        self._filter_changed()

    def _turn_page(self, delta):
        self._page = max(0, self._page + delta)
        self._render()
        self._scroll.verticalScrollBar().setValue(0)

    def _jump_page(self):
        text = self._page_edit.text()
        if text.isdigit():
            self._page = max(0, int(text) - 1)
            self._turn_page(0)

    def _back_to_browser(self):
        if self._busy:
            return
        self._stop_package_download()
        self._tokens["package"] = self._tokens.get("package", 0) + 1
        self._loading_package = False
        self._clear_package_progress()
        self._invalidate()
        self._stack.setCurrentIndex(0)

    def _back(self):
        if self._busy:
            return
        if self._stack.currentIndex() == 1:
            self._back_to_browser()
        elif self._stack.currentIndex() == 0:
            self._turn_page(-1)

    def _forward(self):
        if not self._busy and self._stack.currentIndex() == 0:
            self._turn_page(1)

    def _render(self):
        from Utils.wabbajack.games import matches_game
        query = self._search.text().casefold().strip()
        tag = self._tag.currentData()
        installed = {i.get("gallery_id"): i for i in self._installed if i.get("gallery_id")}
        candidates = [(e, installed.get(e.id)) for e in self._entries]
        if self._only_installed.state():
            from Utils.wabbajack.gallery import GalleryEntry
            feeds = {e.id: e for e in self._entries}
            candidates = []
            for info in self._installed:
                saved = info.get("gallery_metadata", {})
                entry = feeds.get(info.get("gallery_id")) or GalleryEntry(
                    info.get("gallery_id") or "local:" + info["id"], info.get("name", "Modlist"),
                    saved.get("author", "Local installation"), info.get("game", ""), info.get("version", ""),
                    image=saved.get("image", ""), readme=saved.get("readme", ""), community=saved.get("community", ""),
                    download=saved.get("download", ""), nsfw=bool(saved.get("nsfw")), tags=saved.get("tags", []))
                candidates.append((entry, info))
        rows = [(e, info) for e, info in candidates if (self._adult.state() or not e.nsfw)
                and (not query or query in " ".join([e.title, e.author, e.description, e.id, *e.tags]).casefold())
                and (not tag or tag in e.tags) and (not self._featured.state() or e.featured)
                and (not self._hide_unavailable.state() or not e.unavailable)
                and self._game is not None and matches_game(self._game, e.game)]
        if self._sort in {"size", "size_desc"}:
            rows.sort(key=lambda row: (not row[0].install_size,
                row[0].install_size * (-1 if self._sort == "size_desc" else 1), row[0].title.casefold()))
        else:
            rows.sort(key=lambda row: ((not row[0].featured) if self._sort == "featured" else False,
                                       row[0].title.casefold()), reverse=self._sort == "name_desc")
        pages = max(1, (len(rows) + PAGE_SIZE - 1) // PAGE_SIZE)
        self._page = min(self._page, pages - 1)
        for card in self._cards:
            self._grid.removeWidget(card)
            card.hide()
            card.deleteLater()
        self._cards = []
        for entry, info in rows[self._page * PAGE_SIZE:(self._page + 1) * PAGE_SIZE]:
            card = WabbajackCard(entry, info, update_available=bool(info and self._has_update(entry, info)),
                                parent=self._scroll.widget())
            card.activated.connect(lambda e=entry, i=info: self._open_entry(e, i))
            card.context_requested.connect(lambda pos, e=entry, i=info: self._card_menu(e, i, pos))
            self._cards.append(card)
            if entry.image:
                self._request_thumbnail(entry.image)
        self._empty.setText(self.tr("No modlists match these filters.\nTry clearing your search or filters.") if self._game
                            else self.tr("Select a game in the main toolbar to browse its modlists."))
        self._empty.setVisible(not self._cards)
        self._relayout()
        self._page_edit.setText(str(self._page + 1))
        self._page_label.setText(self.tr("of {0}").format(pages))
        self._results_label.setText(self.tr("{0} modlists").format(len(rows)))
        self._prev_button.setEnabled(self._page > 0)
        self._next_button.setEnabled(self._page + 1 < pages)

    def _request_thumbnail(self, url):
        if url not in self._thumb_urls:
            self._thumb_sequence += 1
            self._thumb_urls[url] = self._thumb_sequence
            self._thumb_ids[self._thumb_sequence] = url
        self._thumbs.request(self._thumb_urls[url], url)

    def _thumbnail(self, index, pixmap):
        url = self._thumb_ids.get(index)
        if not url:
            return
        for card in self._cards:
            if card.entry.image == url:
                card.set_thumbnail(pixmap)
        if self._entry and self._entry.image == url:
            self._detail_image.setPixmap(pixmap.scaled(self._detail_image.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation))

    def _relayout(self):
        width = self._scroll.viewport().width() - 32
        columns = max(1, (width + 12) // (CARD_W + 12))
        while self._grid.count():
            self._grid.takeAt(0)
        for index, card in enumerate(self._cards):
            self._grid.addWidget(card, index // columns, index % columns)
            card.show()
        if not self._cards:
            self._grid.addWidget(self._empty, 0, 0)
        self._columns = columns

    def eventFilter(self, watched, event):
        if watched is getattr(self, "_mode", None) and event.type() == QEvent.Wheel:
            event.ignore()
            return True
        if event.type() in {QEvent.Resize, QEvent.Show}:
            scroll = getattr(self, "_scroll", None)
            if scroll and watched is scroll.viewport():
                self._relayout()
            detail = getattr(self, "_detail_scroll", None)
            if detail and watched is detail.viewport():
                direction = QBoxLayout.LeftToRight if detail.viewport().width() >= 850 else QBoxLayout.TopToBottom
                self._detail_columns.setDirection(direction)
            description = getattr(self, "_description", None)
            if description and watched is description.viewport() and event.type() == QEvent.Resize:
                self._fit_description()
        return super().eventFilter(watched, event)

    def _fit_description(self):
        """Size the description box to its wrapped text, between DESC_MIN_H and DESC_MAX_H."""
        box = self._description
        # QPlainTextEdit reports its document height in blocks, not pixels, so measure
        # the wrapped text with a throwaway QTextDocument that honours setTextWidth.
        measure = QTextDocument()
        measure.setDefaultFont(box.font())
        measure.setDocumentMargin(box.document().documentMargin())
        measure.setPlainText(box.toPlainText())
        measure.setTextWidth(max(1, box.viewport().width()))
        margins = box.contentsMargins()
        height = int(measure.size().height()) + margins.top() + margins.bottom() + 2
        height = max(DESC_MIN_H, min(DESC_MAX_H, height))
        if height != box.height():
            box.setFixedHeight(height)

    def _card_menu(self, entry, info, position):
        menu = QMenu(self)
        menu.addAction(self.tr("View modlist"), lambda: self._open_entry(entry, info))
        for text, url in (("Author instructions", entry.readme), ("Community", entry.community)):
            if url.startswith(("https://", "http://")):
                menu.addAction(self.tr(text), lambda url=url: self._external_url(url))
        menu.exec(position)

    def _external_url(self, url):
        from PySide6.QtGui import QDesktopServices
        from PySide6.QtCore import QUrl
        QDesktopServices.openUrl(QUrl(url))

    def _overview(self, title, author, version, game, description, download_size, install_size):
        self._title.setText(title)
        from Utils.wabbajack.games import matches_game
        game_name = self._game.name if self._game and matches_game(self._game, game) else game
        self._detail_author.setText(" · ".join(value for value in [author, version, game_name] if value))
        self._description.setPlainText(description)
        self._description.setVisible(bool(description))
        self._fit_description()
        self._set_figure("download", fmt_size(download_size) if download_size else self.tr("Unknown"))
        self._set_figure("install", fmt_size(install_size) if install_size else self.tr("Unknown"))
        self._refresh_free_space()
        self._detail_sizes.setText("")
        entry = self._entry
        badges = [self.tr("Featured")] if entry and entry.featured else []
        if entry and entry.unavailable:
            badges.append(self.tr("Unavailable"))
        if self._info:
            badges.append(self.tr("Update available") if entry and self._has_update(entry, self._info) else self.tr("Installed"))
        self._detail_tags.setText(" · ".join(badges + (entry.tags if entry else [])))
        self._detail_tags.setVisible(bool(self._detail_tags.text()))

    @staticmethod
    def _fit_list(widget, cap):
        """Size a choice list to its rows so short lists leave no empty gap."""
        rows = sum(widget.sizeHintForRow(row) for row in range(widget.count()))
        widget.setFixedHeight(min(cap, max(24, rows + 4)))

    def _set_figure(self, key, text, tone="TEXT_MAIN"):
        caption, value = self._figures[key]
        value.setText(text)
        value.setStyleSheet(f"color:{_c(active_palette(), tone)}; font-size:16px; font-weight:600;")

    def _refresh_free_space(self):
        """Show free space on the volume the installation directory lives on."""
        import shutil
        target = Path(self._directory.text().strip() or "")
        while target and not target.exists() and target != target.parent:
            target = target.parent
        try:
            free = shutil.disk_usage(target).free if target else 0
        except OSError:
            free = 0
        install = self._package and sum(d.size for d in self._package.directives)
        if not install and self._entry:
            install = self._entry.install_size
        tone = "TEXT_ERR" if free and install and free < install else "TEXT_MAIN"
        self._set_figure("free", fmt_size(free) if free else self.tr("Unknown"), tone)

    @staticmethod
    def _has_update(entry, info):
        if entry.package_hash and info.get("package_xxhash"):
            from Utils.wabbajack.hashes import canonical_hash
            try:
                return canonical_hash(entry.package_hash) != info["package_xxhash"]
            except ValueError:
                pass
        return bool(entry.version and entry.version != info.get("version"))

    def _open_entry(self, entry, info=None):
        if self._busy:
            return
        self._stop_package_download()
        self._tokens["package"] = self._tokens.get("package", 0) + 1
        self._loading_package = False
        self._clear_package_progress()
        self._manual_package = False
        self._entry, self._info = entry, info
        self._overview(entry.title, entry.author, entry.version, entry.game, entry.description,
                       entry.download_size, entry.install_size)
        self._detail_image.setPixmap(icon("Wabbajack.png", 80).pixmap(80, 80))
        if entry.image:
            self._request_thumbnail(entry.image)
        self._package = None
        self._profiles.clear()
        self._task_options.configure(None)
        self._adjustments.clear()
        self._setup_form.setRowVisible(self._adjustments, False)
        self._setup_form.setRowVisible(self._profiles, False)
        self._checks.clear()
        self._setup_hint.setText(self.tr("Check requirements to load the authored profiles and prepare the download plan."))
        self._setup_hint.show()
        self._package_path.setText(self.tr("Saved installation package") if info else self.tr("Package will download from the gallery"))
        self._package_path.setToolTip(info.get("package_path", "") if info else entry.download)
        self._prepare_button.hide()
        self._invalidate()
        self._status.setText(self.tr("This list is currently unavailable for download.") if entry.unavailable else "")
        if info:
            self._directory.setText(info["directory"])
            self._downloads.setText(info.get("downloads", ""))
            mode = "resume" if info.get("status") != "complete" else (
                "update" if self._has_update(entry, info) else "repair")
            self._mode.setCurrentIndex(self._mode.findData(mode))
        else:
            self._mode.setCurrentIndex(0)
            self._game_selected()
        self._stack.setCurrentIndex(1)
        self._detail_scroll.verticalScrollBar().setValue(0)

    def _open_link(self, kind):
        from PySide6.QtGui import QDesktopServices
        from PySide6.QtCore import QUrl
        value = getattr(self._entry, kind, "") if self._entry else ""
        if not value and self._package:
            value = self._package.metadata.get("Readme" if kind == "readme" else "Community", "")
        if value.startswith(("https://", "http://")):
            QDesktopServices.openUrl(QUrl(value))
        elif kind == "readme" and self._package:
            package = self._package
            def read():
                import zipfile
                directive = next((d for d in package.directives if d.data.get("SourceDataID") and
                    ((d.kind == "PropertyFile" and str(d.data.get("Type", "")).lower() in {"1", "readme"})
                     or ("/" not in d.path and d.path.lower().startswith("readme")))), None)
                if directive:
                    with zipfile.ZipFile(package.path) as archive, archive.open(directive.data["SourceDataID"]) as source:
                        data = source.read(512 * 1024 + 1)
                    text = data[:512 * 1024].decode("utf-8-sig", "replace")
                    if len(data) > 512 * 1024:
                        text += "\n[Preview truncated]"
                    return package.identity, text
                return package.identity, value or "No README was supplied in this package."
            self._worker("readme", read)

    def _open_file(self, check_after=False):
        self._check_after_package = bool(check_after)
        self._pick_path("package")

    def _pick_path(self, kind):
        if self._busy:
            return
        from Utils.ui.portal import pick_file, pick_folder
        token = self._tokens.get("package", 0)
        signal = self._path_picked
        def chosen(path):
            safe_emit(signal, kind, token, path)
        if kind == "package":
            pick_file(self.tr("Open Wabbajack modlist"), chosen,
                      filters=[(self.tr("Wabbajack modlists"), ["*.wabbajack"])])
        elif kind == "directory":
            pick_folder(self.tr("Installation directory"), chosen)
        else:
            pick_folder(self.tr("Download directory"), chosen)

    def _on_path_picked(self, kind, token, path):
        if not path or self._busy or token != self._tokens.get("package", 0):
            self._diag("ui.path.result_ignored", kind=kind, path=path,
                       busy=self._busy, token=token,
                       current_token=self._tokens.get("package", 0))
            return
        self._diag("ui.path.selected", kind=kind, path=path)
        if kind == "package":
            check_after = self._check_after_package
            if self._stack.currentIndex() != 1 or not self._info:
                self._entry = self._info = None
                self._mode.setCurrentIndex(0)
                self._game_selected()
            self._inspect(path, check_after=check_after)
        elif kind == "directory":
            self._directory.setText(str(path))
        else:
            self._downloads.setText(str(path))

    def _open_url(self):
        if self._busy:
            return
        from gui_qt.text_input_overlay import TextInputOverlay
        def accepted(value):
            if value and value.startswith(("https://", "http://")):
                self._entry = self._info = None
                self._mode.setCurrentIndex(0)
                self._game_selected()
                self._download_package(value)
        TextInputOverlay.show_over(self, self.tr("Open Wabbajack URL"), self.tr("Direct .wabbajack URL:"), on_done=accepted)

    def _prepare_entry(self, check_after=False):
        if self._busy or self._checking or self._loading_package:
            self._diag("ui.package.prepare_ignored", busy=self._busy,
                       checking=self._checking, loading=self._loading_package)
            return
        if self._manual_package:
            from PySide6.QtGui import QDesktopServices
            from PySide6.QtCore import QUrl
            QDesktopServices.openUrl(QUrl(self._package_url))
            return
        if not self._entry:
            self._open_file(check_after=check_after)
            return
        if self._info and self._mode.currentData() != "update":
            self._inspect(Path(self._info["package_path"]), check_after=check_after)
        elif self._entry.unavailable:
            self._diag("ui.package.unavailable", entry=self._entry.id)
            self._status.setText(self.tr("This list is currently unavailable for download. Open a local .wabbajack file to continue."))
        elif self._entry.download:
            self._download_package(self._entry.download, check_after=check_after)
        else:
            self._diag("ui.package.url_missing", entry=self._entry.id)
            self._status.setText(self.tr("This entry has no package download URL. Open a local .wabbajack file."))
            self._open_file(check_after=check_after)

    def _download_package(self, url, *, check_after=False):
        self._stop_package_download()
        self._package_url = url
        self._manual_package = False
        self._package = None
        self._loading_package = True
        self._package_path.setText(self.tr("Downloading modlist package…"))
        self._package_path.setToolTip("")
        self._invalidate()
        self._check_after_package = check_after
        self._update_start_button()
        from Utils.wabbajack.acquire import download_package
        from Utils.wabbajack.gallery import cache_root
        from Utils.wabbajack.manifest import inspect_package
        import hashlib
        import time
        entry = self._entry
        total = entry.package_size if entry else 0
        token = self._tokens.get("package", 0) + 1
        stop = threading.Event()
        self._package_stop = stop
        self._show_package_progress(0, total)
        self._cancel_package_button.setText(self.tr("Cancel download"))
        self._cancel_package_button.setEnabled(True)
        self._cancel_package_button.show()
        self._status.setText(self.tr("Downloading modlist package…"))
        self._diag("ui.package.download_requested", url=url, expected_bytes=total,
                   expected_hash=entry.package_hash if entry else "",
                   check_after=check_after)
        def work():
            path = cache_root() / (hashlib.sha256(url.encode()).hexdigest() + ".wabbajack")
            last = [0.0]
            def progress(current, maximum):
                now = time.monotonic()
                if now - last[0] >= 0.1 or maximum and current >= maximum:
                    last[0] = now
                    safe_emit(self._progress, "package", (token, current, maximum))
            download_package(url, path, stop=stop, size=total,
                          expected=entry.package_hash if entry else "", progress=progress,
                          log=self._diagnostic_log)
            if stop.is_set():
                raise InterruptedError("Package download stopped")
            safe_emit(self._progress, "package-inspect", (token,))
            return inspect_package(path, log=self._diagnostic_log)
        self._worker("package", work)

    def _inspect(self, path, *, check_after=False):
        self._stop_package_download()
        self._package_stop = None
        self._package_url = ""
        self._manual_package = False
        self._package = None
        self._loading_package = True
        self._package_path.setText(Path(path).name)
        self._package_path.setToolTip(str(path))
        self._clear_package_progress()
        self._invalidate()
        self._check_after_package = check_after
        self._update_start_button()
        self._status.setText(self.tr("Inspecting modlist package…"))
        self._diag("ui.package.inspect_requested", path=path,
                   check_after=check_after)
        from Utils.wabbajack.manifest import inspect_package
        self._worker("package", lambda: inspect_package(path, log=self._diagnostic_log))

    def _stop_package_download(self):
        if self._package_stop is not None:
            self._package_stop.set()

    def _cancel_package_download(self):
        if self._package_stop is None or self._package_stop.is_set():
            return
        self._diag("ui.package.download_cancel_requested", url=self._package_url)
        self._package_stop.set()
        self._cancel_package_button.setText(self.tr("Cancelling…"))
        self._cancel_package_button.setEnabled(False)
        self._status.setText(self.tr("Cancelling modlist package download…"))
        self._footer_hint.setText(self.tr("Stopping the download. Any partial download will be kept so it can resume later."))

    def _game_selected(self, *_):
        game = self._game
        if game and not self._info:
            from Utils.config_paths import get_download_cache_dir_for_game
            self._directory.setText(str(Path(game.get_profile_root()) / ".wabbajack" / uuid.uuid4().hex))
            self._downloads.setText(str(get_download_cache_dir_for_game(game.name)))
        self._invalidate()

    def _path_row(self, parent, field, callback, placeholder):
        """One-line elided path display backed by a hidden line edit."""
        row = QWidget(parent)
        box = QHBoxLayout(row)
        box.setContentsMargins(0, 0, 0, 0)
        box.setSpacing(8)
        label = ElidedPathLabel(placeholder, row)
        box.addWidget(label, 1)
        self._button("Change…", callback, box)
        field.textChanged.connect(label.set_path)
        label.set_path(field.text())
        return row

    def _choose_directory(self):
        self._pick_path("directory")

    def _choose_downloads(self):
        self._pick_path("downloads")

    def _options_changed(self, *_):
        self._invalidate(preserve_report=True)

    def _invalidate(self, *_, preserve_report=False):
        keep_report = preserve_report and self._report is not None
        self._check_after_package = False
        self._preflight_stop.set()
        self._tokens["preflight"] = self._tokens.get("preflight", 0) + 1
        self._checking = False
        if hasattr(self, "_task_options") and hasattr(self, "_profiles"):
            self._task_options.set_profiles(self._selected_choices(self._profiles))
        if not keep_report:
            self._report = None
        if not self._busy:
            self._request = None
            if keep_report:
                self._status.setText(self.tr("Options changed. Previous requirements are shown for reference; recheck to update them."))
            else:
                self._checks.clear()
                self._acquisition_summary.clear()
            self._texture_button.hide()
        self._update_start_button()

    def _set_step(self, active):
        """Highlight progress through check → review → install."""
        palette = active_palette()
        for index, step in enumerate(self._steps):
            done = index < active
            current = index == active
            tone = "ACCENT" if current else ("TEXT_MAIN" if done else "TEXT_FAINT")
            weight = "600" if current or done else "400"
            step.setStyleSheet(f"color:{_c(palette, tone)}; font-weight:{weight};")

    def _set_setup_state(self, text, tone="TEXT_DIM"):
        palette = active_palette()
        self._setup_state.setText(text)
        self._setup_state.setStyleSheet(f"color:{_c(palette, tone)}; background:{_c(palette, 'BG_ROW')}; padding:5px 9px; border-radius:4px; font-weight:600;")

    def _show_package_progress(self, current, total):
        current, total = max(0, int(current or 0)), max(0, int(total or 0))
        if total:
            self._package_progress.setRange(0, 1000)
            self._package_progress.setValue(min(1000, int(current * 1000 / total)))
            self._package_progress_text.setText(self.tr("{0} / {1} ({2}%)").format(
                fmt_size(min(current, total)), fmt_size(total), min(100, int(current * 100 / total))))
        else:
            self._package_progress.setRange(0, 0)
            self._package_progress_text.setText(
                self.tr("{0} downloaded").format(fmt_size(current)) if current else self.tr("Starting…"))
        self._package_progress.show()
        self._package_progress_text.show()

    def _clear_package_progress(self):
        self._package_progress.hide()
        self._package_progress_text.hide()
        self._cancel_package_button.hide()
        self._cancel_package_button.setText(self.tr("Cancel download"))
        self._cancel_package_button.setEnabled(True)
        self._package_progress.setRange(0, 1000)
        self._package_progress.setValue(0)
        self._package_progress_text.clear()

    def _update_start_button(self):
        available = bool(self._manual_package or self._entry and (
            not self._entry.unavailable or self._info and self._mode.currentData() != "update"))
        idle = not (self._busy or self._checking or self._loading_package or self._installing_mpi or self._installing_texture)
        self._start_button.setEnabled(idle and bool(self._package or available))
        self._prepare_button.setEnabled(idle and available)
        self._choose_package_button.setEnabled(idle)
        self._texture_button.setEnabled(idle)
        ready = self._request is not None and self._report is not None and self._report.ok
        stale = self._request is None and self._report is not None
        operation = self.tr("Install") if self._mode.currentData() == "install" else self._mode.currentText()
        if self._busy:
            label = self.tr("Installing…")
        elif self._installing_texture or self._installing_mpi:
            label = self.tr("Preparing tool…")
        elif self._loading_package:
            label = self.tr("Loading modlist…")
        elif self._checking:
            label = self.tr("Checking requirements…")
        elif ready:
            label = operation
        elif self._manual_package and not self._package:
            label = self.tr("Choose .wabbajack…")
        else:
            label = self.tr("Recheck requirements") if self._report is not None else self.tr("Check requirements")
        self._start_button.setText(label)
        self._start_button.setToolTip(self.tr("Start the selected operation using the reviewed download plan.") if ready
                                     else self.tr("Load the package if needed, check requirements, and prepare the download plan for review."))
        if self._busy:
            self._set_setup_state(self.tr("Installing"), "ACCENT")
            self._set_step(2)
            self._footer_hint.setText(self.tr("Installation is running. Pause and cancel are available in the progress window."))
        elif self._installing_texture or self._installing_mpi:
            self._set_setup_state(self.tr("Preparing tool…"), "ACCENT")
            self._set_step(0)
            self._footer_hint.setText(self.tr("Wait for tool setup to finish, then check requirements again."))
        elif self._checking or self._loading_package:
            self._set_setup_state(self.tr("Checking…") if self._checking else self.tr("Loading…"), "ACCENT")
            self._set_step(0)
            self._footer_hint.setText(self.tr("Checking game files, downloads and available space…") if self._checking
                                      else self.tr("Loading the package, then checking requirements…") if self._check_after_package
                                      else self.tr("Loading the modlist's profiles and options…"))
        elif stale:
            self._set_setup_state(self.tr("Recheck required"), "TEXT_WARN")
            self._set_step(0)
            self._footer_hint.setText(self.tr("Options changed. Previous requirements are shown for reference; recheck to update them."))
        elif self._report is not None and not self._report.ok:
            blocking = sum(c.status == "error" for c in self._report.checks)
            self._set_setup_state(self.tr("1 blocking") if blocking == 1
                                  else self.tr("{0} blocking").format(blocking), "TEXT_ERR")
            self._set_step(1)
            self._footer_hint.setText(self.tr("Resolve the blocking requirement to continue, then recheck.") if blocking == 1
                                      else self.tr("Resolve the {0} blocking requirements to continue, then recheck.").format(blocking))
        elif ready:
            self._set_setup_state(self.tr("Ready to install") if self._mode.currentData() == "install" else self.tr("Plan ready"), "ACCENT")
            self._set_step(1)
            self._footer_hint.setText(self.tr("Review the requirements and download plan, then select {0}.").format(operation))
        else:
            self._set_setup_state(self.tr("Package ready") if self._package else self.tr("Not checked"))
            self._set_step(0)
            self._footer_hint.setText(self.tr("Check requirements and review the download plan."))

    def _primary_action(self):
        if self._busy or self._checking or self._loading_package:
            self._diag("ui.primary_action.ignored", busy=self._busy,
                       checking=self._checking, loading=self._loading_package)
            return
        if self._request is not None and self._report is not None and self._report.ok:
            self._start()
        else:
            self._check()

    def _install_texconv(self):
        if self._busy or self._installing_texture or self._installing_mpi:
            self._diag("ui.texture_setup.ignored", busy=self._busy,
                       texture_running=self._installing_texture,
                       mpi_running=self._installing_mpi)
            return
        from types import SimpleNamespace
        request = SimpleNamespace(texconv=None, proton=None, setup_options=self._task_options.values(),
                                  directory=Path(self._directory.text()).expanduser())
        self._invalidate()
        self._installing_texture = True
        self.setEnabled(False)
        self._task_options.setEnabled(False)
        self.running_changed.emit(True)
        self._update_start_button()
        from Utils.wabbajack.textures import install_texture_tool
        self._status.setText(self.tr("Preparing the isolated texture runtime and testing DDS conversion…"))
        self._worker("texture", lambda: install_texture_tool(self._worker_stop, request=request,
            log=lambda message: safe_emit(self._progress, "log", (message,))))

    def _can_install_mpi(self):
        return bool(self._game and not (
            self._busy or self._checking or self._loading_package
            or self._installing_mpi or self._installing_texture or self._shutting_down)
            and self._can_install())

    def _mpi_running_changed(self, running):
        self._installing_mpi = running
        if self._shutting_down:
            return
        if running:
            self._invalidate()
        self.running_changed.emit(running)
        self._update_start_button()

    def _mpi_ready(self, _exe):
        if not self._shutting_down:
            self._status.setText(self.tr(
                "Native MPI tool installed. Check requirements again to verify the selected package."))

    def _check(self):
        if self._busy or self._checking or self._loading_package or self._installing_mpi or self._installing_texture:
            self._diag("ui.preflight.ignored", busy=self._busy,
                       checking=self._checking, loading=self._loading_package,
                       mpi_running=self._installing_mpi,
                       texture_running=self._installing_texture)
            return
        if not self._package:
            self._diag("ui.preflight.package_required",
                       manual_package=self._manual_package,
                       gallery_entry=getattr(self._entry, "id", None))
            if self._manual_package:
                self._open_file(check_after=True)
            else:
                self._prepare_entry(check_after=True)
            return
        if not self._can_install():
            self._diag("ui.preflight.operation_blocked")
            self._status.setText(self.tr("Wait for the current install or deployment operation to finish."))
            return
        from Utils.wabbajack.models import InstallRequest
        from Utils.wabbajack.games import source_roots
        from Utils.wabbajack.preflight import preflight
        game = self._game
        if not game:
            self._diag("ui.preflight.game_missing")
            self._status.setText(self.tr("Configure the required game before installing this modlist."))
            return
        profiles = self._selected_choices(self._profiles)
        api = self._get_api()
        request = InstallRequest(self._package, copy.copy(game), Path(self._directory.text()).expanduser(),
            Path(self._downloads.text()).expanduser(), profiles, source_roots(self._package, game), api=api,
            fixes=self._selected_choices(self._adjustments),
            setup_options=self._task_options.values(),
            mode=self._mode.currentData(), gallery_id=self._entry.id if self._entry and not self._entry.id.startswith("local:") else "")
        from Utils.wabbajack.store import installation_info
        info = installation_info(request.directory, self._diagnostic_log)
        if info:
            for name, path in info.get("source_roots", {}).items():
                if name not in request.game_roots or not request.game_roots[name].is_dir():
                    request.game_roots[name] = Path(path)
            request.gallery_id = request.gallery_id or info.get("gallery_id", "")
        if self._entry:
            request.gallery_metadata = {k: getattr(self._entry, k) for k in ("author", "image", "readme", "community", "download", "nsfw", "tags")}
        self._request = request
        self._diag("ui.preflight.requested", diagnostic_id=request.diagnostic_id,
                   operation=request.mode, package=request.package.name,
                   profiles=profiles, fixes=request.fixes,
                   setup_options=request.setup_options)
        self._report = None
        self._preflight_stop = stop = threading.Event()
        self._checking = True
        self._update_start_button()
        self._checks.setPlainText(self.tr("Checking requirements…"))
        self._acquisition_summary.clear()
        self._texture_button.hide()
        self._status.setText(self.tr("Checking game files, downloads, disk space, and runtime requirements…"))
        def work():
            def progress(*args):
                safe_emit(self._progress, "preflight", (request, *args))
            progress("Checking Nexus account", 0, 0, "")
            if api:
                try:
                    request.premium = bool(api.validate().is_premium)
                    self._diag("ui.nexus.account_validated", premium=request.premium)
                except Exception as exc:
                    emit_exception(self._diagnostic_log, "ui.nexus.account_validation_failed",
                                   exc, diagnostic_id=request.diagnostic_id)
                    from Utils.ui.config import load_nexus_last_premium
                    request.premium = bool(load_nexus_last_premium())
                    self._diag("ui.nexus.account_cached", premium=request.premium)
            from Utils.ui.config import load_force_manual_install
            if load_force_manual_install():
                request.premium = False
                self._diag("ui.nexus.manual_forced")
            return request, preflight(request, stop,
                                      progress=progress, log=self._diagnostic_log)
        self._worker("preflight", work)

    def _start(self):
        if self._busy or self._checking or self._loading_package or not self._request or not self._report or not self._report.ok:
            self._diag("ui.install.ignored", busy=self._busy,
                       checking=self._checking, loading=self._loading_package,
                       request=bool(self._request), report=bool(self._report),
                       preflight_ok=bool(self._report and self._report.ok))
            return
        if not self._can_install():
            self._diag("ui.install.operation_blocked",
                       diagnostic_id=self._request.diagnostic_id)
            self._status.setText(self.tr("Wait for the current install or deployment operation to finish."))
            return
        self._busy = True
        self._diag("ui.install.requested", diagnostic_id=self._request.diagnostic_id,
                   operation=self._request.mode, package=self._request.package.name)
        self._update_start_button()
        self.running_changed.emit(True)
        self._control = InstallControl()
        from Utils.ui.config import (
            load_collection_settings, _MAX_EXTRACT_WORKERS_CEILING)
        collection_settings = load_collection_settings()
        extract_workers = collection_settings["max_extract_workers"]
        self._control.extract_workers.set_limit(extract_workers)
        self._answers = queue.Queue()
        self._request.resolve_conflicts = self._wait_conflicts
        from gui_qt.collection_install_overlay import CollectionInstallOverlay
        from Utils.downloads.bandwidth import set_limit_mbps
        from Utils.ui.config import load_download_speed_limit
        self._overlay = CollectionInstallOverlay.show_over(self, self._package.name,
            on_pause=self._pause, on_cancel=self._cancel, on_limit_change=set_limit_mbps,
            limit_mbps=load_download_speed_limit(), hide_completed_batches=True,
            install_heading=self.tr("Installing / Reconstructing"),
            extract_workers=extract_workers,
            max_extract_workers=_MAX_EXTRACT_WORKERS_CEILING,
            on_extract_workers_change=self._set_extract_workers)
        callbacks = InstallCallbacks(on_log=lambda text: safe_emit(self._progress, "log", (text,)),
                                    on_manual_mod=lambda payload: safe_emit(self._manual, payload))
        slots = {"on_status": "set_status", "on_phase": "set_phase", "on_display_total": "set_display_total",
            "on_mod_plan": "set_mod_plan", "on_row_installed": "row_installed",
            "on_agg_download": "set_agg", "on_dl_mod_start": "dl_start", "on_dl_mod_update": "dl_update",
            "on_dl_mod_finish": "dl_finish", "on_extract_add": "extract_add", "on_extract_remove": "extract_remove",
            "on_extract_queue": "extract_queue", "on_extract_wait": "extract_wait",
            "on_extract_update": "extract_update"}
        for attribute, slot in slots.items():
            setattr(callbacks, attribute, lambda *args, slot=slot: safe_emit(self._progress, slot, args))
        from Utils.wabbajack.install import run_install
        self._worker("install", lambda: run_install(self._request, callbacks=callbacks,
                      control=self._control, report=self._report))

    def _on_progress(self, method, args):
        if method == "package-inspect":
            token, = args
            if token == self._tokens.get("package") and self._loading_package:
                self._package_stop = None
                self._cancel_package_button.hide()
                self._package_path.setText(self.tr("Download complete · inspecting package…"))
                self._status.setText(self.tr("Modlist package downloaded. Checking its contents…"))
            return
        if method == "package":
            token, current, total = args
            if token == self._tokens.get("package") and self._loading_package:
                self._show_package_progress(current, total)
                if total and current >= total:
                    self._package_path.setText(self.tr("Download complete · inspecting package…"))
                    self._status.setText(self.tr("Modlist package downloaded. Checking its contents…"))
                else:
                    self._status.setText(self.tr("Downloading modlist package: {0}").format(
                        self._package_progress_text.text()))
            return
        if method == "preflight":
            request, phase, current, total, detail = args
            if not self._shutting_down and self._checking and request is self._request:
                text = phase + (": " + detail if detail else "…")
                self._status.setText(text)
                self._footer_hint.setText(text)
            return
        if method == "log":
            self._log("[wabbajack] " + args[0])
            return
        if self._overlay:
            getattr(self._overlay, method)(*args)
        if method == "dl_finish" and self._manual_overlay and args[0] == self._manual_row:
            self._manual_auto_open = self._manual_overlay._auto_open_chk.isChecked()
            self._manual_overlay.dismiss()
            self._manual_overlay = None

    def _on_manual(self, payload):
        if self._shutting_down:
            return
        self._manual_row = payload["idx"]
        from gui_qt.collection_manual_overlay import CollectionManualOverlay
        if self._manual_overlay is None:
            self._manual_overlay = CollectionManualOverlay.show_over(self, self._package.name, "",
                len(self._package.archives), self._control.manual_queue, on_pause=self._pause, on_cancel=self._cancel)
            self._manual_overlay._auto_open_chk.setChecked(self._manual_auto_open)
            self._manual_overlay._seen_first = self._manual_auto_open
        self._manual_overlay.update_mod(payload)

    def _pause(self):
        self._diag("ui.operation.pause_requested",
                   diagnostic_id=getattr(self._request, "diagnostic_id", ""))
        self._control.pause.set()
        self._control.stop.set()
        self._answers.put(None)

    def _set_extract_workers(self, value: int):
        self._control.extract_workers.set_limit(value)
        try:
            from Utils.ui.config import save_max_extract_workers
            save_max_extract_workers(value)
        except Exception:
            pass

    def _cancel(self):
        self._diag("ui.operation.cancel_requested",
                   diagnostic_id=getattr(self._request, "diagnostic_id", ""))
        self._control.cancel.set()
        self._control.stop.set()
        self._answers.put(None)

    def _wait_conflicts(self, conflicts):
        safe_emit(self._conflicts, conflicts)
        while not self._control.stop.is_set():
            try:
                return self._answers.get(timeout=0.2)
            except queue.Empty:
                pass
        return None

    def _review_conflicts(self, conflicts):
        if self._shutting_down:
            return
        if self._overlay:
            self._overlay.hide()
        self._conflict_table.setRowCount(len(conflicts))
        for row, conflict in enumerate(conflicts):
            item = QTableWidgetItem(conflict.path)
            item.setData(Qt.UserRole, conflict)
            self._conflict_table.setItem(row, 0, item)
            self._conflict_table.setItem(row, 1, QTableWidgetItem(conflict.reason))
            choice = CappedComboBox(self._conflict_table)
            choice.addItem(self.tr("Keep mine"), "keep")
            choice.addItem(self.tr("Use author version"), "author")
            self._conflict_table.setCellWidget(row, 2, choice)
        self._stack.setCurrentIndex(2)
        if conflicts:
            self._conflict_table.setCurrentCell(0, 0)
            self._show_conflict(0)

    def _show_conflict(self, row, *_):
        item = self._conflict_table.item(row, 0) if row >= 0 else None
        conflict = item.data(Qt.UserRole) if item else None
        if not conflict:
            return
        summary = f"{conflict.path}\nOriginal author: {conflict.old_hash or 'absent'}\nInstalled: {conflict.current_hash or 'absent'}\nNew author: {conflict.new_hash or 'absent'}\n\n"
        def read(path):
            if not path or not Path(path).is_file():
                return "", False
            with Path(path).open("rb") as stream:
                data = stream.read(65537)
            if b"\0" in data:
                raise UnicodeError("Binary file")
            return data[:65536].decode("utf-8-sig"), len(data) > 65536
        try:
            import difflib
            mine, cut_mine = read(conflict.current_path)
            author, cut_author = read(conflict.author_path)
            diff = list(difflib.unified_diff(mine.splitlines(), author.splitlines(), fromfile="Installed", tofile="Author", lineterm=""))
            summary += "\n".join(diff[:2000])
            if cut_mine or cut_author or len(diff) > 2000:
                summary += "\n\n" + self.tr("Preview truncated. Review the complete files before choosing.")
        except (UnicodeError, OSError):
            summary += self.tr("Binary or unreadable content. Compare the recorded hashes and file locations.")
        summary += f"\n\nInstalled file: {conflict.current_path or 'absent'}\nAuthored file: {conflict.author_path or 'absent'}"
        self._conflict_details.setPlainText(summary)

    def _set_choices(self, index):
        for row in range(self._conflict_table.rowCount()):
            self._conflict_table.cellWidget(row, 2).setCurrentIndex(index)

    def _accept_conflicts(self):
        choices = {self._conflict_table.item(row, 0).text(): self._conflict_table.cellWidget(row, 2).currentData()
                   for row in range(self._conflict_table.rowCount())}
        sample = dict(list(choices.items())[:50])
        self._diag("ui.conflicts.accepted", choices=len(choices), sample=sample,
                   sample_truncated=len(choices) > 50)
        self._answers.put(choices)
        self._stack.setCurrentIndex(1)
        if self._overlay:
            self._overlay.show()

    def _received(self, kind, result, error):
        if self._shutting_down:
            return
        token, result = result
        if token != self._tokens.get(kind):
            self._diag("ui.worker.result_ignored", kind=kind, token=token,
                       current_token=self._tokens.get(kind))
            return
        self._diag("ui.worker.result_received", kind=kind, token=token,
                   error=error, result_type=type(result).__name__ if result else None)
        if kind == "texture":
            self.setEnabled(True)
            self._installing_texture = False
            self.running_changed.emit(False)
            self._task_options.setEnabled(True)
            self._update_start_button()
        if kind == "package":
            check_after = self._check_after_package
            package_cancelled = self._package_stop is not None and self._package_stop.is_set()
            self._package_stop = None
            self._check_after_package = False
            self._loading_package = False
            self._clear_package_progress()
            self._update_start_button()
        if kind == "preflight":
            self._checking = False
            self._update_start_button()
            if self._request is None:
                return
            if error:
                self._checks.setPlainText(error)
        if kind == "gallery":
            self._loading_overlay.hide_overlay()
            self._refresh_button.setEnabled(True)
            if error and not self._cards:
                self._empty.setText(self.tr("The gallery could not be loaded. Try Refresh, or open a local .wabbajack file."))
        if kind == "package" and error and package_cancelled:
            self._diag("ui.package.download_cancelled", url=self._package_url)
            self._package_path.setText(self.tr("Download cancelled"))
            self._status.setText(self.tr("Modlist package download cancelled."))
            self._footer_hint.setText(self.tr("Select Check requirements to resume; any partial download will be reused."))
            return
        if error:
            self._status.setText(error)
            self._log("[wabbajack] " + error)
            if kind == "package" and self._package_url.startswith(("http://", "https://")):
                self._manual_package = True
                self._prepare_button.setText(self.tr("Open download page"))
                self._prepare_button.show()
                self._checks.setPlainText(self.tr("Download the .wabbajack file from {0}, then use Open .wabbajack to continue setup.").format(self._package_url))
                self._stack.setCurrentIndex(1)
            if kind == "package":
                self._update_start_button()
            if kind in {"package", "preflight", "install"}:
                self._set_setup_state(self.tr("Could not continue"), "TEXT_ERR")
                self._footer_hint.setText(self.tr("Review the error details, then try the operation again."))
        if kind == "gallery" and result:
            self._entries = result.entries
            self.gallery_changed.emit(result)
            self._refresh_tags()
            self._status.setText(self.tr("Using cached gallery information.") if result.cached else self.tr("Gallery loaded."))
            if result.warnings:
                self._status.setText(self._status.text() + self.tr(" {0} feeds unavailable.").format(len(result.warnings)))
                self._log("[wabbajack] " + "\n".join(result.warnings))
            self._render()
        elif kind == "readme" and result and self._package and result[0] == self._package.identity:
            self._description.setPlainText(result[1])
            self._description.show()
            self._fit_description()
        elif kind == "package" and result:
            self._package = result
            self._overview(result.name, str(result.metadata.get("Author", "")), result.version, result.game,
                str(result.metadata.get("Description", "")) or (self._entry.description if self._entry else ""),
                sum(a.size for a in result.archives.values()), sum(d.size for d in result.directives))
            if not self._entry:
                self._detail_image.setPixmap(icon("Wabbajack.png", 80).pixmap(80, 80))
            self._prepare_button.setVisible(bool(self._entry))
            self._prepare_button.setText(self.tr("Reload"))
            self._package_path.setText(result.path.name if not (len(result.path.stem) == 64 and all(c in "0123456789abcdef" for c in result.path.stem))
                                       else self.tr("{0} · saved package").format(result.name))
            self._package_path.setToolTip(str(result.path))
            self._detail_sizes.setText(
                self.tr("{0} archives · {1} files").format(
                    f"{len(result.archives):,}",
                    f"{len(result.directives):,}"))
            self._refresh_free_space()
            self._setup_hint.hide()
            self._setup_form.setRowVisible(self._profiles, True)
            self._profiles.clear()
            selected = set(self._info.get("selected_profiles", [])) if self._info else set(result.profiles)
            if self._info and self._mode.currentData() == "update":
                selected.update(set(result.profiles) - set(self._info.get("authored_profiles", [])))
            for profile in result.profiles:
                self._add_choice(self._profiles, profile, profile, profile in selected)
            self._fit_list(self._profiles, 110)
            self._adjustments.clear()
            self._task_options.configure(result, self._info.get("setup_options", {}) if self._info else {}, game=self._game)
            from Utils.wabbajack.runtime import adjustments
            game = self._game
            if game:
                accepted = self._info.get("fixes", []) if self._info else []
                try:
                    available_adjustments = adjustments(result, game)
                except (OSError, ValueError, KeyError, configparser.Error, zipfile.BadZipFile) as exc:
                    available_adjustments = []
                    self._log("[wabbajack] " + str(exc))
                for adjustment in available_adjustments:
                    self._add_choice(self._adjustments, adjustment.label, adjustment.id, adjustment.id in accepted)
            self._fit_list(self._adjustments, 100)
            self._setup_form.setRowVisible(self._adjustments, bool(self._adjustments.count()))
            self._stack.setCurrentIndex(1)
            if not self._info and not self._directory.text().strip():
                self._game_selected()
            self._invalidate()
            if check_after:
                self._check()
            else:
                self._status.setText(self.tr("Package ready. Check requirements to prepare the download plan."))
                QTimer.singleShot(0, self, lambda: self._detail_scroll.ensureWidgetVisible(self._profiles))
        elif kind == "preflight" and result:
            request, report = result
            if request is not self._request:
                return
            self._report = report
            self._log("[wabbajack] Requirements checked in " + f"{sum(report.timings.values()):.2f}s: "
                      + "; ".join(f"{name}: {seconds:.2f}s" for name, seconds in report.timings.items()))
            self._checks.show_report(report)
            self._acquisition_summary.show_report(request, report)
            self._texture_button.setVisible(any(c.name == "Texture conversion" and c.status == "error" for c in report.checks))
            self._update_start_button()
            self._status.setText(self.tr("Requirements and download plan ready. Review them before starting.") if report.ok
                                 else self.tr("Resolve the listed requirements, then recheck."))
            if report.ok:
                QTimer.singleShot(0, self, lambda: self._detail_scroll.ensureWidgetVisible(self._acquisition_summary))
            else:
                QTimer.singleShot(0, self, lambda: self._detail_scroll.ensureWidgetVisible(self._checks))
        elif kind == "texture" and result:
            self._status.setText(self.tr("Texture tool installed. Check requirements again to refresh the download plan."))
        elif kind == "install":
            request = self._request
            self._busy = False
            self.running_changed.emit(False)
            for overlay in (self._overlay, self._manual_overlay):
                if overlay:
                    overlay.dismiss()
            self._overlay = self._manual_overlay = None
            self._stack.setCurrentIndex(1)
            self._invalidate()
            self._refresh_installed()
            self._render()
            from Utils.wabbajack.store import installation_info
            self._info = installation_info(request.directory, self._diagnostic_log)
            if self._info:
                self._mode.setCurrentIndex(self._mode.findData("repair" if self._info.get("status") == "complete" else "resume"))
            if result:
                self._status.setText(result.message)
                instructions = self._info.get("remaining_instructions", []) if self._info and result.status == "complete" else []
                self._checks.setPlainText("\n\n".join([result.message, *instructions]))
                self._set_setup_state({"complete": self.tr("Installed"), "paused": self.tr("Paused"), "cancelled": self.tr("Cancelled")}.get(result.status, self.tr("Needs attention")))
                self._footer_hint.setText(self.tr("Installation complete. You can select its profiles from the main toolbar.") if result.status == "complete"
                                          else self.tr("Your progress is saved. Check requirements, then select Resume to continue."))
                if result.status == "complete":
                    self.installed.emit(request.game, result)
            if error:
                self._checks.setPlainText(error)
                self._set_setup_state(self.tr("Could not continue"), "TEXT_ERR")
                self._footer_hint.setText(self.tr("Review the error details, then try the operation again."))

    def tab_closing(self):
        self._stop_package_download()
        self._tokens["package"] = self._tokens.get("package", 0) + 1
        self._loading_package = False
        self._clear_package_progress()
        self._invalidate()
        self._pause()
        for overlay in (self._overlay, self._manual_overlay):
            if overlay:
                overlay.dismiss()

    def tab_close_blocked(self):
        blocked = self._busy or self._installing_mpi or self._installing_texture
        if blocked:
            self._status.setText(self.tr("Wait for tool setup to finish before closing this tab.") if self._installing_mpi or self._installing_texture
                                 else self.tr("Pause or cancel the installation before closing this tab."))
        return blocked

    def _refresh_tags(self):
        from Utils.wabbajack.games import matches_game
        selected = self._tag.currentData()
        tags = {tag for entry in self._entries if self._game and matches_game(self._game, entry.game) for tag in entry.tags}
        self._tag.blockSignals(True)
        self._tag.clear()
        self._tag.addItem(self.tr("All tags"), "")
        for tag in sorted(tags, key=str.casefold):
            self._tag.addItem(tag, tag)
        self._tag.setCurrentIndex(max(0, self._tag.findData(selected)))
        self._tag.blockSignals(False)

    def set_game(self, game):
        if self._busy:
            self._diag("ui.game.change_ignored", current=getattr(self._game, "name", None),
                       requested=getattr(game, "name", None), busy=True)
            return
        previous = self._game
        self._game = game
        self._diag("ui.game.changed", previous=getattr(previous, "name", None),
                   previous_id=getattr(previous, "game_id", None),
                   current=getattr(game, "name", None),
                   current_id=getattr(game, "game_id", None))
        self._stop_package_download()
        self._tokens["package"] = self._tokens.get("package", 0) + 1
        self._loading_package = False
        self._clear_package_progress()
        self._entry = self._info = self._package = None
        self._manual_package = False
        self._package_url = ""
        self._profiles.clear()
        self._adjustments.clear()
        self._checks.clear()
        self._directory.clear()
        self._downloads.clear()
        self._current_game.setText(game.name if game else self.tr("No game selected"))
        self._target_game.setText(game.name if game else self.tr("Select a game in the main toolbar"))
        self._refresh_installed()
        self._search.clear()
        self._tag.setCurrentIndex(0)
        self._refresh_tags()
        self._game_selected()
        self._stack.setCurrentIndex(0)
        self._filter_changed()
        self._load_gallery()
