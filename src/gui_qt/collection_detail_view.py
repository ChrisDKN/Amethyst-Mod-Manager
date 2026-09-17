
from __future__ import annotations

import threading

from PySide6.QtCore import Qt, Signal, QT_TRANSLATE_NOOP, QAbstractTableModel, QModelIndex, QCoreApplication
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QFrame,
    QScrollArea, QCheckBox, QComboBox, QTableView, QSizePolicy,
    QHeaderView, QAbstractItemView,
)

from gui_qt.theme_qt import active_palette, _c
from gui_qt.safe_emit import safe_emit
from gui_qt.worker import run_in_worker
from Utils.collections.manifest import fmt_size


class CollectionModModel(QAbstractTableModel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._mods = []
        self._column = 0
        self._order = Qt.AscendingOrder

    def set_mods(self, mods):
        self.beginResetModel()
        self._mods = list(mods)
        self._sort()
        self.endResetModel()

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self._mods)

    def columnCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(_COLS)

    def _values(self, mod):
        return (CollectionDetailView._display_name(mod), mod.mod_author or "",
                mod.version or "", int(mod.size_bytes or 0), bool(mod.optional))

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid() or not 0 <= index.row() < len(self._mods):
            return None
        values = self._values(self._mods[index.row()])
        if role == Qt.ToolTipRole:
            return values[0]
        if role == Qt.DisplayRole:
            value = values[index.column()]
            if index.column() == 3:
                return fmt_size(value)
            if index.column() == 4:
                return "✓" if value else ""
            return value
        return None

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if orientation == Qt.Horizontal and role == Qt.DisplayRole and 0 <= section < len(_COLS):
            return QCoreApplication.translate("CollectionDetailView", _COLS[section])
        return None

    def sort(self, column, order=Qt.AscendingOrder):
        if not 0 <= column < len(_COLS):
            return
        self.beginResetModel()
        self._column, self._order = column, order
        self._sort()
        self.endResetModel()

    def _sort(self):
        def key(mod):
            value = self._values(mod)[self._column]
            return value.casefold() if isinstance(value, str) else value
        self._mods.sort(key=key, reverse=self._order == Qt.DescendingOrder)


# Slug registry of collections whose install is paused mid-run (mirrors Tk's
# module-level ``_PAUSED_INSTALLS``). A live pause adds the slug here so an open
# detail view's button flips to "Resume" without re-reading the profile; a
# persisted ``collection_install_paused`` flag covers reopen-after-restart.
_PAUSED_COLLECTIONS: "set[str]" = set()


class _RevisionCombo(QComboBox):
    """A QComboBox whose popup is HARD-capped in height + scrolls, so a collection
    with hundreds of revisions never opens a full-screen-tall list. Capping the
    view alone isn't enough - the popup CONTAINER (view.window()) sizes to content
    - so we clamp it after Qt lays it out AND re-anchor it just below the button
    (a very tall popup gets centred on the cursor by default)."""

    _MAX_POPUP_H = 340        # ~14 rows

    def wheelEvent(self, event):
        event.ignore()

    def showPopup(self):
        super().showPopup()
        try:
            popup = self.view().window()
            if popup is None:
                return
            capped = popup.height() > self._MAX_POPUP_H
            if capped:
                popup.setFixedHeight(self._MAX_POPUP_H)
            # Anchor below the combo (default centring only kicks in for a popup
            # too tall to fit below; after capping we want it under the button).
            if capped:
                from PySide6.QtCore import QPoint
                below = self.mapToGlobal(QPoint(0, self.height()))
                x, y = below.x(), below.y()
                scr = self.screen()
                if scr is not None:
                    g = scr.availableGeometry()
                    x = max(g.left(), min(x, g.right() - popup.width()))
                    # If it would overflow the bottom, open upward from the top.
                    if y + popup.height() > g.bottom():
                        above = self.mapToGlobal(QPoint(0, 0))
                        y = max(g.top(), above.y() - popup.height())
                popup.move(x, y)
        except Exception:
            pass

# Mod-list columns.
# Translated at display time (setHorizontalHeaderLabels); register for lupdate.
_COLS = [
    QT_TRANSLATE_NOOP("CollectionDetailView", "Name"),
    QT_TRANSLATE_NOOP("CollectionDetailView", "Author"),
    QT_TRANSLATE_NOOP("CollectionDetailView", "Version"),
    QT_TRANSLATE_NOOP("CollectionDetailView", "Size"),
    QT_TRANSLATE_NOOP("CollectionDetailView", "Opt"),
]
_COL_SIZE = 3


class CollectionDetailView(QWidget):
    """*api* (authed NexusAPI), *collection* (NexusCollection), *game*. Optional
    *log_fn*, *on_install(chosen_fids, skipped_fids)* (install is stubbed)."""

    _detail_ready = Signal(object)      # (name, size, count, mods, dl_path, revisions) | None
    _manifest_ready = Signal(object)    # (token, offsite list[(name, url)], manifest dict|None)
    convert_requested = Signal(str)
    title_resolved = Signal(str)        # real collection name once the detail loads

    def __init__(self, api, collection, game, log_fn=None, on_install=None,
                 revision_number=None, local_manifest=None, bundle_zip=None,
                 allow_append=False, parent=None):
        super().__init__(parent)
        self._api = api
        self._collection = collection
        self._game = game
        self._log = log_fn or (lambda _m: None)
        self._on_install = on_install
        # An explicitly opened collection (including an additional-domain NXM
        # link) must be fetched from its own host domain. The game domain is
        # only a fallback for cards that omit it.
        self._domain = (getattr(collection, "game_domain", "")
                        or getattr(game, "nexus_game_domain", "") or "")
        self._mods = []
        self._offsite: list[tuple[str, str]] = []   # (name, url) - manual downloads
        self._total_size = 0                        # collection totalSize+assetsSizeBytes
        self._dl_path = ""                          # collection-archive download link
        # Local-manifest import: populate from a parsed manifest dict instead of the
        # API, and (optionally) restore bundled mods + profile files from a local
        # .amethyst zip after install. Forces a NEW profile (no revision on Nexus).
        self._local_manifest = local_manifest
        self._bundle_zip_path = str(bundle_zip) if bundle_zip else ""
        # Manifest fetched by _start_manifest_fetch, kept for the install worker
        # (Tk parity: CollectionsDialog._collection_schema_cache). Without it the
        # orchestrator re-downloads the manifest at install time; if that second
        # CDN fetch fails it silently loses every FOMOD/BAIN auto-selection.
        self._fetched_manifest: "dict | None" = None
        self._fetched_manifest_rev: "int | None" = None
        # Imports normally force a NEW profile (a .amethyst bundle carries profile
        # state - plugins/saves - that can't be safely merged). A code import has
        # no bundle, so the caller may pass allow_append=True to permit appending
        # into an existing profile.
        self._recommend_new_profile = bool(local_manifest) and not allow_append
        self._opt_boxes: list[tuple[QCheckBox, int]] = []   # (checkbox, file_id)
        self._optional_reuse_profile = ""
        self._optional_before_reuse = {}
        self._revision_number = revision_number    # None = latest published
        # A ctor-requested revision (e.g. Open Current) - the FIRST fetch is still
        # done at "latest" so the revisions list + dropdown populate, then we
        # switch to this one.
        self._pending_initial_rev = revision_number
        self._revisions_list: list[dict] = []
        self._detail_token = 0                     # guards stale revision fetches
        self._unsupported_collection_schema = False
        self._game_versions: list[str] = []
        self._data_ready = local_manifest is not None
        self._image_token = 0

        self.setObjectName("CollectionDetailView")
        self._detail_ready.connect(self._on_detail_ready)
        self._manifest_ready.connect(self._on_manifest_ready)
        self._build()
        if self._local_manifest is not None:
            self._populate_from_local_manifest()
        else:
            self._start_detail_fetch()

    # -- local-manifest import ---------------------------------------------
    def _populate_from_local_manifest(self):
        """Fill the mod table + off-site panel from a parsed local manifest dict
        (no API). Port of the Tk CollectionsDialog._fetch_from_local_manifest."""
        from Nexus.nexus_api import NexusCollectionMod as _NCM
        cj = self._local_manifest or {}
        info = cj.get("info") or {}
        if not isinstance(info, dict):
            info = {}
        self._game_versions = [
            str(version or "").strip()
            for version in (info.get("gameVersions") or [])
            if str(version or "").strip()
        ]
        schema_mods = cj.get("mods", [])
        mods = []
        total_size = 0
        offsite: list[tuple[str, str]] = []
        for m in schema_mods:
            src = m.get("source") or {}
            src_type = (src.get("type") or "nexus").lower()
            mod_name = m.get("name") or ""
            fid = int(src.get("fileId") or 0)
            mid = int(src.get("modId") or 0)
            file_size = int(src.get("fileSize") or 0)
            total_size += file_size
            if src.get("bundle") is True or src_type == "bundle":
                mods.append(_NCM(mod_name=mod_name,
                                 file_name=mod_name, source_type="bundle"))
                continue
            if src_type == "thunderstore":
                # Listed so the table + size label show the whole profile, but
                # installed by a separate pass (app._install_thunderstore_entries)
                # - the Nexus orchestrator is keyed on integer file ids a
                # Thunderstore package does not have. install_mods() filters
                # these out for exactly that reason.
                mods.append(_NCM(
                    mod_name=mod_name,
                    file_name=(src.get("fullName") or mod_name),
                    size_bytes=file_size, source_type="thunderstore",
                    version=(src.get("version") or ""),
                    optional=bool(m.get("optional", False))))
                continue
            if src_type in ("browse", "direct"):
                url = src.get("url") or src.get("fileUrl") or ""
                if url:
                    offsite.append((mod_name, url))
                continue
            cat = m.get("category") or {}
            mods.append(_NCM(
                mod_id=mid, file_id=fid, mod_name=mod_name,
                file_name=src.get("logicalFilename") or mod_name,
                size_bytes=file_size, optional=bool(m.get("optional", False)),
                source_type="nexus", version=m.get("version") or "",
                update_policy=(src.get("updatePolicy") or "exact").lower(),
                category_id=int(cat.get("id") or 0),
                category_name=(cat.get("name") or "").strip(),
                domain_name=(m.get("domainName") or "").strip()))
        self._mods = mods
        self._total_size = int(total_size or 0)
        self._size_lbl.setText(self.tr("{0} mods").format(f"{len(mods):,}"))
        self._refresh_figures()
        self._fill_table()
        self._fill_optional()
        # Optional flags already came straight from the manifest - no override.
        self._on_manifest_ready((self._detail_token, offsite, None))

    # -- construction -------------------------------------------------------
    def _panel(self, title):
        panel = QFrame(self)
        panel.setObjectName("CollectionPanel")
        palette = active_palette()
        panel.setStyleSheet(
            f"#CollectionPanel {{ background:{_c(palette, 'BG_PANEL')};"
            f" border:1px solid {_c(palette, 'BORDER')}; border-radius:6px; }}")
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)
        if title:
            label = QLabel(title, panel)
            label.setStyleSheet("font-weight:600;")
            layout.addWidget(label)
        return panel, layout

    def _build(self):
        from gui_qt.collapsible_section import CollapsibleSection
        from gui_qt.collection_setup import CollectionSetup
        from gui_qt.nexus_mod_card import ThumbnailLoader
        p = active_palette()
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        self._scroll = QScrollArea(self)
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.NoFrame)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        page = QWidget(self._scroll)
        content = QVBoxLayout(page)
        content.setContentsMargins(16, 12, 16, 12)
        content.setSpacing(12)
        self._scroll.setWidget(page)
        root.addWidget(self._scroll, 1)

        overview, ov = self._panel("")
        self._overview_panel = overview
        content.addWidget(overview)
        self._overview_row = QHBoxLayout()
        self._overview_row.setSpacing(18)
        ov.addLayout(self._overview_row)
        self._image = QLabel(self.tr("No image"), overview)
        self._image.setFixedSize(248, 140)
        self._image.setAlignment(Qt.AlignCenter)
        self._image.setStyleSheet(f"background:{_c(p, 'BG_DEEP')}; color:{_c(p, 'TEXT_DIM')}; border-radius:6px;")
        self._overview_row.addWidget(self._image, 0, Qt.AlignTop)
        self._overview_text = QWidget(overview)
        text = QVBoxLayout(self._overview_text)
        text.setContentsMargins(0, 0, 0, 0)
        text.setSpacing(6)
        self._overview_row.addWidget(self._overview_text, 1)
        self._title_lbl = QLabel(self._collection.name or self._collection.slug, overview)
        self._title_lbl.setTextFormat(Qt.PlainText)
        self._title_lbl.setWordWrap(True)
        self._title_lbl.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self._title_lbl.setStyleSheet("font-size:22px; font-weight:700;")
        text.addWidget(self._title_lbl)
        self._author_lbl = QLabel(overview)
        self._author_lbl.setTextFormat(Qt.PlainText)
        self._author_lbl.setWordWrap(True)
        self._author_lbl.setStyleSheet(f"color:{_c(p, 'TEXT_DIM')};")
        text.addWidget(self._author_lbl)
        self._summary_lbl = QLabel(overview)
        self._summary_lbl.setTextFormat(Qt.PlainText)
        self._summary_lbl.setWordWrap(True)
        self._summary_lbl.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self._summary_lbl.setStyleSheet(f"color:{_c(p, 'TEXT_DIM')};")
        text.addWidget(self._summary_lbl)
        self._rev_selector = _RevisionCombo(overview)
        self._rev_selector.setMinimumWidth(140)
        self._rev_selector.setMaximumWidth(220)
        self._rev_selector.setMaxVisibleItems(14)
        self._rev_selector.setVisible(False)
        self._rev_updating = False
        self._rev_selector.currentIndexChanged.connect(self._on_revision_index)
        text.addWidget(self._rev_selector)
        figures = QHBoxLayout()
        figures.setSpacing(22)
        self._figures = {}
        for key, title in (("download", self.tr("Download")),
                           ("free", self.tr("Free space"))):
            cell = QVBoxLayout()
            cell.setSpacing(0)
            caption = QLabel(title, overview)
            caption.setStyleSheet(
                f"color:{_c(p, 'TEXT_FAINT')}; font-size:10px; font-weight:600;")
            cell.addWidget(caption)
            value = QLabel("—", overview)
            value.setTextFormat(Qt.PlainText)
            value.setStyleSheet(
                f"color:{_c(p, 'TEXT_MAIN')}; font-size:16px; font-weight:600;")
            cell.addWidget(value)
            figures.addLayout(cell)
            self._figures[key] = value
        self._size_lbl = QLabel(self.tr("Loading…"), overview)
        self._size_lbl.setWordWrap(True)
        self._size_lbl.setStyleSheet(f"color:{_c(p, 'TEXT_DIM')};")
        figures.addWidget(self._size_lbl, 1, Qt.AlignBottom)
        text.addLayout(figures)
        self._thumbs = ThumbnailLoader(self, crop_w=248, crop_h=372, fit=True)
        self._thumbs.loaded.connect(self._on_thumbnail)
        self._refresh_overview()

        self._setup_panel, setup_layout = self._panel(self.tr("Installation"))
        self._setup = CollectionSetup(self._game, self._collection, self._setup_panel)
        self._setup.convert_requested.connect(self.convert_requested)
        setup_layout.addWidget(self._setup)
        content.addWidget(self._setup_panel)

        self._opt_panel, opt = self._panel("")
        self._opt_section = CollapsibleSection(self.tr("Optional mods"))
        self._opt_section.set_expanded(True)
        opt.addWidget(self._opt_section)
        opt = QVBoxLayout(self._opt_section.body)
        opt.setContentsMargins(0, 0, 0, 0)
        self._opt_host = QWidget()
        self._opt_scroll = self._opt_host
        self._opt_layout = QVBoxLayout(self._opt_host)
        self._opt_layout.setContentsMargins(0, 0, 0, 0)
        self._opt_layout.setSpacing(5)
        self._opt_empty = QLabel(self.tr("Loading…"), self._opt_host)
        self._opt_layout.addWidget(self._opt_empty)
        self._opt_layout.addStretch(1)
        opt.addWidget(self._opt_host)
        row = QHBoxLayout()
        self._select_all_btn = QPushButton(self.tr("Select all"), self._opt_panel)
        self._deselect_all_btn = QPushButton(self.tr("Deselect all"), self._opt_panel)
        for button, checked in ((self._select_all_btn, True), (self._deselect_all_btn, False)):
            button.setObjectName("FormButton")
            button.clicked.connect(lambda _=False, value=checked: self._set_all_optional(value))
            row.addWidget(button)
        row.addStretch(1)
        opt.addLayout(row)
        content.addWidget(self._opt_panel)
        self._opt_panel.hide()

        self._offsite_panel, offsite = self._panel("")
        self._offsite_wrap = self._offsite_panel
        self._offsite_title = QLabel(self.tr("Off-site mods"), self._offsite_panel)
        self._offsite_title.setStyleSheet(f"color:{_c(p, 'TEXT_WARN')}; font-weight:600;")
        self._offsite_title.setWordWrap(True)
        offsite.addWidget(self._offsite_title)
        self._offsite_scroll = QScrollArea(self._offsite_panel)
        self._offsite_scroll.setWidgetResizable(True)
        self._offsite_scroll.setFrameShape(QFrame.NoFrame)
        self._offsite_scroll.setMinimumHeight(60)
        self._offsite_scroll.setMaximumHeight(230)
        self._offsite_host = QWidget()
        self._offsite_layout = QVBoxLayout(self._offsite_host)
        self._offsite_layout.setContentsMargins(0, 0, 0, 0)
        self._offsite_layout.addStretch(1)
        self._offsite_scroll.setWidget(self._offsite_host)
        offsite.addWidget(self._offsite_scroll)
        content.addWidget(self._offsite_panel)
        self._offsite_panel.hide()

        panel, table_layout = self._panel("")
        self._mods_section = CollapsibleSection(self.tr("Mods"))
        table_layout.addWidget(self._mods_section)
        table_body = QVBoxLayout(self._mods_section.body)
        table_body.setContentsMargins(0, 0, 0, 0)
        self._table = QTableView(self._mods_section.body)
        self._mod_model = CollectionModModel(self._table)
        self._table.setModel(self._mod_model)
        self._table.setSortingEnabled(True)
        self._table.sortByColumn(0, Qt.AscendingOrder)
        self._table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self._table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.SingleSelection)
        self._table.setWordWrap(False)
        self._table.verticalHeader().hide()
        self._table.verticalHeader().setSectionResizeMode(QHeaderView.Fixed)
        self._table.setAlternatingRowColors(True)
        self._table.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._table.verticalHeader().setDefaultSectionSize(self.fontMetrics().height() + 10)
        header = self._table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.Stretch)
        for column, width in ((1, 140), (2, 85), (3, 95), (4, 45)):
            header.setSectionResizeMode(column, QHeaderView.Interactive)
            header.resizeSection(column, width)
        self._table.setStyleSheet(
            f"QTableView {{ background:{_c(p, 'BG_LIST')}; alternate-background-color:{_c(p, 'BG_ROW_ALT')};"
            f" color:{_c(p, 'TEXT_MAIN')}; gridline-color:{_c(p, 'BORDER')}; }}")
        table_body.addWidget(self._table)
        content.addWidget(panel)
        content.addStretch(1)

        footer = QWidget(self)
        footer.setObjectName("HeaderBar")
        actions = QHBoxLayout(footer)
        actions.setContentsMargins(16, 10, 16, 10)
        view = QPushButton(self.tr("View on Nexus"), footer)
        view.setObjectName("FormButton")
        view.clicked.connect(self._open_on_nexus)
        view.setVisible(bool(self._collection.slug and self._domain))
        actions.addWidget(view)
        actions.addStretch(1)
        self._install_btn = QPushButton(self.tr("Install collection"), footer)
        self._install_btn.setObjectName("PrimaryButton")
        self._install_btn.clicked.connect(self._on_install_clicked)
        actions.addWidget(self._install_btn)
        root.addWidget(footer)
        self._install_intent = "install"
        self._setup.changed.connect(self._update_install_btn_state)
        self._setup.changed.connect(self._refresh_figures)
        self.refresh_install_options()

    def _refresh_overview(self):
        col = self._collection
        self._author_lbl.setText(self.tr("by {0}").format(col.user_name) if col.user_name else "")
        self._author_lbl.setVisible(bool(col.user_name))
        self._summary_lbl.setText(col.summary or "")
        self._summary_lbl.setVisible(bool(col.summary))
        url = col.tile_image_url or ""
        if url and url != getattr(self, "_image_url", ""):
            self._image_url = url
            self._image_token += 1
            self._image.clear()
            self._image.setText(self.tr("No image"))
            self._image.setFixedSize(248, 140)
            self._thumbs.request(self._image_token, url)

    def _on_thumbnail(self, token, pixmap):
        if token == self._image_token and pixmap is not None and not pixmap.isNull():
            self._image.setFixedSize(pixmap.size())
            self._image.setPixmap(pixmap)
            self._fit_overview_height()

    def _set_figure(self, key, text, tone="TEXT_MAIN"):
        value = self._figures[key]
        value.setText(text)
        value.setStyleSheet(
            f"color:{_c(active_palette(), tone)}; font-size:16px; font-weight:600;")

    def _free_space_target(self):
        options = self._setup.options()
        if options.mode == "append" and options.target:
            from Utils.collections.grouping import profile_path
            from Utils.profiles.state import profile_uses_specific_mods
            profile = profile_path(self._game, options.target)
            if profile_uses_specific_mods(profile):
                return profile / "mods"
            return self._game.get_mod_staging_path()
        if options.mode == "group" and options.reuse_profile:
            from Utils.collections.grouping import profile_path
            return profile_path(self._game, options.reuse_profile) / "mods"
        return self._game.get_profile_root() / "profiles"

    def _refresh_figures(self):
        if not hasattr(self, "_figures"):
            return
        import shutil
        self._set_figure(
            "download", fmt_size(self._total_size) if self._total_size else self.tr("Unknown"))
        try:
            target = self._free_space_target()
        except (OSError, ValueError):
            target = self._game.get_profile_root() / "profiles"
        while target and not target.exists() and target != target.parent:
            target = target.parent
        try:
            free = shutil.disk_usage(target).free if target else 0
        except OSError:
            free = 0
        tone = "TEXT_ERR" if free and self._total_size and free < self._total_size else "TEXT_MAIN"
        self._set_figure("free", fmt_size(free) if free else self.tr("Unknown"), tone)

    def _fit_overview_height(self):
        if not hasattr(self, "_overview_panel"):
            return
        narrow = self.width() < 650
        text_width = max(200, self.width() - (60 if narrow else self._image.width() + 78))
        text_height = self._overview_text.layout().totalHeightForWidth(text_width)
        image_height = self._image.height()
        self._overview_panel.setMinimumHeight(
            image_height + max(100, text_height) + 30 if narrow
            else max(image_height, max(100, text_height)) + 24)
        self._overview_row.invalidate()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        narrow = self.width() < 650
        self._overview_row.setDirection(QHBoxLayout.TopToBottom if narrow else QHBoxLayout.LeftToRight)
        self._fit_overview_height()

    def refresh_install_options(self):
        self._setup.refresh(self._domain, self._resolved_viewing_revision(), self._recommend_new_profile)

    def install_options(self):
        return self._setup.options()

    # -- fetch: detail ------------------------------------------------------
    def _start_detail_fetch(self):
        self._detail_token += 1
        token = self._detail_token
        self._data_ready = False
        self._total_size = 0
        self._refresh_figures()
        self._mods_section.set_expanded(False)
        self._install_btn.setEnabled(False)
        slug = getattr(self._collection, "slug", "") or ""
        domain = self._domain
        # First fetch (no revisions list yet) is always "latest" so the dropdown
        # populates; a ctor-requested revision is applied afterwards.
        rev = (None if (not self._revisions_list
                        and self._pending_initial_rev is not None)
               else self._revision_number)

        run_in_worker(
            lambda: (token, self._api.get_collection_detail(
                slug, domain, revision_number=rev)),
            self._detail_ready, name="collection-detail",
            error_result=(token, None))

    def _on_detail_ready(self, payload):
        token, result = payload
        if token != self._detail_token:
            return                       # a newer revision switch superseded this
        if result is None:
            self._size_lbl.setText(self.tr("Could not load collection."))
            self._opt_empty.setText(self.tr("Could not load."))
            return
        name, total_size, mod_count, mods, dl_path, revisions, card = result
        self._dl_path = dl_path or ""   # collection-archive download link (manifest)
        # The bare NexusCollection built for NXM / "Open Current" only knows the
        # slug, so the header + tab initially show the id-like slug. Now that the
        # real name has arrived, update both.
        if name and getattr(self, "_title_lbl", None) is not None:
            self._title_lbl.setText(name)
            self._collection.name = name
            self.title_resolved.emit(name)
        if isinstance(card, dict) and card.get("unsupported_collection_schema"):
            self._unsupported_collection_schema = True
            schema_id = int(card.get("collection_schema_id") or 0)
            if schema_id == 2:
                message = self.tr(
                    "This is a Wabbajack list and cannot be installed by "
                    "Amethyst. Install it with Wabbajack instead.")
            else:
                message = self.tr(
                    "This collection uses an unsupported format and cannot "
                    "be installed by Amethyst.")
            self._mods = []
            self._total_size = 0
            self._mod_model.set_mods(())
            self._size_lbl.setText(message)
            self._refresh_figures()
            self._opt_empty.setText(self.tr("No installable collection data."))
            self._install_btn.setText(self.tr("Unsupported collection"))
            self._install_btn.setToolTip(message)
            self._install_btn.setEnabled(False)
            return
        self._unsupported_collection_schema = False
        self._data_ready = False
        self._install_btn.setEnabled(False)
        self._install_btn.setToolTip("")
        self._game_versions = [
            str(version or "").strip()
            for version in ((card or {}).get("game_versions") or [])
            if str(version or "").strip()
        ]
        # Enrich the (possibly bare NXM/"Open Current") collection with the
        # display fields we just fetched, so an append records a full card
        # (image + stats) into installed_collections/<slug>.json.
        try:
            if mod_count:
                self._collection.mod_count = int(mod_count)
            if isinstance(card, dict):
                if card.get("tile_image_url") and not getattr(
                        self._collection, "tile_image_url", ""):
                    self._collection.tile_image_url = card["tile_image_url"]
                if card.get("total_downloads"):
                    self._collection.total_downloads = int(card["total_downloads"])
                if card.get("endorsements"):
                    self._collection.endorsements = int(card["endorsements"])
        except Exception:
            pass
        self._refresh_overview()
        # `revisions` is populated only on the latest fetch (empty on a specific
        # revision fetch) - don't clobber the stored list.
        if revisions:
            self._revisions_list = list(revisions)
            self._populate_revision_dropdown()
        # A ctor-requested revision: now that the dropdown exists, switch to it
        # (unless it's already the just-loaded latest).
        if self._pending_initial_rev is not None and self._revisions_list:
            want = self._pending_initial_rev
            self._pending_initial_rev = None
            latest = self._latest_published_rev(self._revisions_list)
            if want != latest:
                self._revision_number = want
                self._set_rev_current(want)
                self._mod_model.set_mods(())
                self._size_lbl.setText(self.tr("Loading…"))
                self._start_detail_fetch()
                return
            self._revision_number = want
        self._mods = list(mods or [])
        self._total_size = int(total_size or 0)
        self._size_lbl.setText(self.tr("{0} mods").format(f"{mod_count:,}"))
        self._refresh_figures()
        self._fill_table()
        self._fill_optional()
        # Now lazily fetch the manifest (for off-site) - cache-first.
        rev = (self._revision_number if self._revision_number is not None
               else self._latest_published_rev(self._revisions_list))
        self._start_manifest_fetch(dl_path, rev)

    # -- revision picker ----------------------------------------------------
    def _installed_revision(self):
        """The revisionNumber currently installed for this collection (from the
        profile that has it), or None. Small file reads - UI thread is fine."""
        slug = getattr(self._collection, "slug", "") or ""
        if not slug or self._game is None:
            return None
        try:
            from Utils.games.registry import find_profile_with_collection_slug
            from Utils.profiles.state import read_collection_revision
            pname = find_profile_with_collection_slug(self._game.name, slug)
            if not pname:
                return None
            pdir = self._game.get_profile_root() / "profiles" / pname
            return read_collection_revision(pdir)
        except Exception:
            return None

    def _collection_profile(self):
        """(profile_name, profile_dir) of the profile holding this collection, or
        (None, None). Uses slug match so any revision suffix counts."""
        slug = getattr(self._collection, "slug", "") or ""
        if not slug or self._game is None:
            return None, None
        try:
            from Utils.games.registry import find_profile_with_collection_slug
            pname = find_profile_with_collection_slug(self._game.name, slug)
            if not pname:
                return None, None
            return pname, self._game.get_profile_root() / "profiles" / pname
        except Exception:
            return None, None

    def _is_paused(self) -> bool:
        """True if this collection's install is paused (in-memory registry or the
        persisted ``collection_install_paused`` flag on its profile).

        Either source only counts when the collection's profile still exists - a
        deleted/imported-then-removed profile leaves a stale slug in the in-memory
        registry, which would otherwise flip the button to "Resume Install" and
        error on click. When the profile is gone we discard the stale slug so the
        button falls back to "Install"."""
        slug = getattr(self._collection, "slug", "") or ""
        _pname, pdir = self._collection_profile()
        if pdir is None or not pdir.is_dir():
            # Profile no longer exists - any paused state is meaningless.
            if slug:
                _PAUSED_COLLECTIONS.discard(slug)
            return False
        if slug and slug in _PAUSED_COLLECTIONS:
            return True
        try:
            from Utils.profiles.state import read_collection_install_paused
            return bool(read_collection_install_paused(pdir))
        except Exception:
            return False

    def _resolved_viewing_revision(self):
        """The revision the user is currently viewing - the explicit dropdown
        selection, else the highest published revision. None if not loaded yet."""
        if self._revision_number is not None:
            try:
                return int(self._revision_number)
            except (TypeError, ValueError):
                return None
        return self._latest_published_rev(self._revisions_list)

    def _update_available(self) -> bool:
        """True if an installed copy exists AND is pinned to a different revision
        than the one being viewed. Legacy installs (revision None) never qualify."""
        installed = self._installed_revision()
        if installed is None:
            return False
        viewing = self._resolved_viewing_revision()
        if viewing is None:
            return False
        return int(viewing) != int(installed)

    def _update_install_btn_state(self):
        """Set the install button text + intent based on collection state.
        Priority: Resume (paused) > Update (revision differs) > Install."""
        btn = getattr(self, "_install_btn", None)
        if btn is None:
            return
        if self._unsupported_collection_schema:
            btn.setText(self.tr("Unsupported collection"))
            btn.setEnabled(False)
            return
        from Utils.ui.config import load_download_only
        try:
            dl_only = bool(load_download_only())
        except Exception:
            dl_only = False
        if dl_only:
            # Resume/update both need a profile a download-only run never creates.
            self._install_intent = "install"
            btn.setText(self.tr("Download collection"))
            self._setup_panel.hide()
            btn.setEnabled(self._data_ready)
            return
        self._setup_panel.setVisible(True)
        btn.setEnabled(self._data_ready and self._setup.valid())
        options = self._setup.options()
        reused = options.reuse_profile if options.mode == "group" else ""
        if reused != self._optional_reuse_profile:
            if not self._optional_reuse_profile:
                self._optional_before_reuse = {fid: cb.isChecked() for cb, fid in self._opt_boxes}
            self._optional_reuse_profile = reused
            saved = self._saved_skipped_fids() if reused else set()
            for cb, fid in self._opt_boxes:
                cb.setChecked(fid not in saved if reused else self._optional_before_reuse.get(fid, cb.isChecked()))
        self._opt_panel.setEnabled(not reused)
        if self._setup.options().mode == "group":
            self._install_intent = "group"
            options = self._setup.options()
            from Utils.collections.grouping import pending_group, profile_path
            retry = options.reuse_profile and pending_group(profile_path(self._game, options.reuse_profile))
            btn.setText(self.tr("Retry grouping") if retry else (
                self.tr("Group collection") if options.reuse_profile else self.tr("Install and group")))
            return
        if self._setup.options().mode == "append":
            self._install_intent = "install"
            btn.setText(self.tr("Append collection"))
        elif self._is_paused():
            self._install_intent = "resume"
            btn.setText(self.tr("Resume Install"))
        elif self._update_available():
            self._install_intent = "update"
            btn.setText(self.tr("Update Collection"))
        else:
            self._install_intent = "install"
            btn.setText(self.tr("Install collection"))

    def showEvent(self, event):
        super().showEvent(event)
        # Refresh on (re)show so a paused/updated state is reflected on reopen.
        self.refresh_install_options()

    def _populate_revision_dropdown(self):
        installed = self._installed_revision()
        want = (self._revision_number if self._revision_number is not None
                else self._latest_published_rev(self._revisions_list))
        revs = sorted(self._revisions_list,
                      key=lambda r: int(r.get("revisionNumber") or 0),
                      reverse=True)
        self._rev_updating = True          # suppress currentIndexChanged
        self._rev_selector.clear()
        current_idx = 0
        for i, r in enumerate(revs):
            num = r.get("revisionNumber", "?")
            status = (r.get("revisionStatus") or "")
            label = self.tr("Rev {0}").format(num)
            if status and status.lower() != "published":
                label += " ({0})".format(status.lower())
            try:
                if installed is not None and int(num) == int(installed):
                    label += " " + self.tr("(installed)")
            except (TypeError, ValueError):
                pass
            # Store the raw revision int as item data (avoids re-parsing).
            try:
                self._rev_selector.addItem(label, int(num))
            except (TypeError, ValueError):
                self._rev_selector.addItem(label, None)
            try:
                if want is not None and int(num) == int(want):
                    current_idx = i
            except (TypeError, ValueError):
                pass
        if self._rev_selector.count():
            self._rev_selector.setCurrentIndex(current_idx)
        self._rev_updating = False
        self._rev_selector.setVisible(self._rev_selector.count() > 0)
        self._update_install_btn_state()

    def _set_rev_current(self, rev_num):
        """Select the entry for *rev_num* without firing the change handler."""
        idx = self._rev_selector.findData(int(rev_num))
        if idx >= 0:
            self._rev_updating = True
            self._rev_selector.setCurrentIndex(idx)
            self._rev_updating = False

    def _on_revision_index(self, idx: int):
        if self._rev_updating or idx < 0:
            return
        rev_num = self._rev_selector.itemData(idx)
        if rev_num is None or rev_num == self._revision_number:
            return
        self._revision_number = int(rev_num)
        # Reset the panels; the next detail fetch reloads them for this revision.
        self._offsite = []
        self._offsite_wrap.setVisible(False)
        self._mod_model.set_mods(())
        self._total_size = 0
        self._refresh_figures()
        self._size_lbl.setText(self.tr("Loading…"))
        self._update_install_btn_state()     # viewing rev changed → maybe Update
        self._start_detail_fetch()

    @staticmethod
    def _display_name(m) -> str:
        """The name to show for a collection mod. Prefer ``file_name`` - the
        per-file GraphQL ``file.name`` (e.g. "3c - Terran Armada - 256 Textures")
        - which is distinct per file. ``mod_name`` is ``file.mod.name``, the
        SHARED mod-page name, so several files from one page look identical
        (GH #282). ``file_name`` is always a display-quality label here (not an
        archive filename); fall back to ``mod_name`` only if it's empty."""
        return (getattr(m, "file_name", "") or getattr(m, "mod_name", "")
                or (f"Mod {getattr(m, 'mod_id', 0)}"))

    def _fill_table(self):
        self._mod_model.set_mods(self._mods)
        self._mods_section._toggle.setText(self.tr("Mods ({0})").format(len(self._mods)))
        rows = self._mod_model.rowCount()
        height = (self._table.horizontalHeader().sizeHint().height()
                  + rows * self._table.verticalHeader().defaultSectionSize()
                  + self._table.frameWidth() * 2)
        self._table.setFixedHeight(max(40, height))

    def _fill_optional(self):
        # In-session choices: keep the user's unticks when the checklist is
        # rebuilt (revision switch, manifest-override refresh).
        prior_fids = {fid for _cb, fid in self._opt_boxes if fid}
        prior_unticked = {fid for cb, fid in self._opt_boxes
                          if fid and not cb.isChecked()}
        # Clear the placeholder + any prior boxes.
        while self._opt_layout.count() > 2:      # keep the trailing stretch
            it = self._opt_layout.takeAt(1)
            w = it.widget()
            if w is not None:
                w.setParent(None)
                w.deleteLater()
        self._opt_boxes = []
        # Thunderstore entries are excluded: this checklist is keyed on Nexus
        # file ids, so every one of them would share the key 0 - and the
        # Thunderstore install pass does not consult the skip set anyway, so a
        # box here would be a choice that silently does nothing.
        optionals = [m for m in self._mods if m.optional
                     and getattr(m, "source_type", "") != "thunderstore"]
        has_opt = bool(optionals)
        self._select_all_btn.setEnabled(has_opt)
        self._deselect_all_btn.setEnabled(has_opt)
        self._opt_panel.setVisible(has_opt)
        self._opt_empty.setVisible(not has_opt)
        if not has_opt:
            self._opt_empty.setText(self.tr("No optional mods."))
            return
        # Selections saved by the last install of this collection (Tk parity:
        # pre_skipped_fids) - only consulted for boxes not shown this session.
        saved_skipped = self._saved_skipped_fids()
        for i, m in enumerate(optionals):
            name = self._display_name(m)
            cb = QCheckBox(name)
            if m.file_id in prior_fids:
                cb.setChecked(m.file_id not in prior_unticked)
            else:
                cb.setChecked(m.file_id not in saved_skipped)
            cb.setToolTip(name)
            self._opt_layout.insertWidget(i + 1, cb)
            self._opt_boxes.append((cb, m.file_id))

    def _saved_skipped_fids(self) -> "set[int]":
        """Optional mods unticked on the LAST install of this collection, read
        from the profile that holds it. Empty set when none is saved."""
        if self._optional_reuse_profile:
            from Utils.collections.grouping import profile_path
            pdir = profile_path(self._game, self._optional_reuse_profile)
        else:
            _pname, pdir = self._collection_profile()
        if pdir is None or not pdir.is_dir():
            return set()
        try:
            from Utils.profiles.state import read_collection_optional_skipped
            return read_collection_optional_skipped(pdir)
        except Exception:
            return set()

    def _set_all_optional(self, checked: bool):
        for cb, _fid in self._opt_boxes:
            cb.setChecked(checked)

    @staticmethod
    def _latest_published_rev(revisions):
        try:
            published = [
                int(r.get("revisionNumber") or 0)
                for r in (revisions or [])
                if (r.get("revisionStatus") or "").lower() == "published"
            ]
            return max(published) if published else None
        except Exception:
            return None

    # -- fetch: manifest (lazy, cache-first) --------------------------------
    def _start_manifest_fetch(self, dl_path, rev):
        if not dl_path:
            return
        slug = getattr(self._collection, "slug", "") or ""
        game_name = getattr(self._game, "name", "") or ""
        # Stamp the current detail token so a manifest that lands after the user
        # has switched revisions is dropped (see _on_manifest_ready). Without
        # this an old revision's manifest could overwrite the new one's names.
        token = self._detail_token

        def worker():
            offsite = []
            manifest = {}
            try:
                from Utils.collections.manifest import (
                    load_collection_manifest, extract_offsite_mods)
                manifest = load_collection_manifest(
                    self._api, game_name, slug, rev, dl_path, log_fn=self._log)
                offsite = extract_offsite_mods(manifest)
            except Exception as exc:
                self._log(f"Collection manifest error: {exc}")
            if not manifest:
                # An empty manifest means the .7z download failed or the archive
                # had no collection.json. The mod table then keeps the generic
                # GraphQL page names (files from one mod page all look alike);
                # log it so that looks like a fetch failure, not a naming bug.
                self._log(
                    f"Collection: manifest empty for {slug!r} rev={rev} - "
                    f"per-file names not applied (using mod-page names).")
            safe_emit(self._manifest_ready, (token, offsite, manifest))

        threading.Thread(target=worker, daemon=True,
                         name="collection-manifest").start()

    def _apply_manifest_overrides(self, manifest) -> bool:
        """Override the optional flag + the ambiguous display name on each mod
        using collection.json as the authoritative source. Returns True if
        anything changed.

        The GraphQL mod list is unreliable in two ways this repairs:
          * it sometimes marks non-optional mods as optional; and
          * its per-file name comes from ``file.mod.name``, which is the SHARED
            mod-page name - so every file from one page shows the same text
            (the six identical "…Textures" rows). Worse, when GraphQL can't
            resolve ``file.mod`` (adult/moderated/partial responses) it returns
            ``modId=0`` and an EMPTY name; that's the intermittent case where
            names never de-duplicate.

        The manifest's ``name`` is the clean per-file label (its
        ``logicalFilename`` is the archive-derived one, occasionally uglier -
        e.g. hyphens collapsed to spaces - so it's only a fallback). We apply it
        whenever the current name is ambiguous: empty, or shared by >1 file in
        this collection. Unique, non-empty GraphQL names are left untouched."""
        info: "dict[int, tuple[bool, str, str]]" = {}
        for cm in (manifest or {}).get("mods", []):
            src = cm.get("source") or {}
            fid = src.get("fileId")
            if fid is not None:
                cj_name = (cm.get("name") or src.get("logicalFilename") or "")
                policy = (src.get("updatePolicy") or "exact").lower()
                if policy not in ("exact", "prefer", "latest"):
                    policy = "exact"
                info[int(fid)] = (bool(cm.get("optional", False)), cj_name, policy)
        if not info:
            return False
        # An ambiguous display name is one that is empty (GraphQL couldn't
        # resolve the mod page) or shared by more than one file in this
        # collection (all files from a single mod page). Counting by NAME rather
        # than mod_id makes this robust to the modId=0 null-mod case.
        name_counts: "dict[str, int]" = {}
        for m in self._mods:
            nm = m.mod_name or ""
            name_counts[nm] = name_counts.get(nm, 0) + 1
        changed = False
        for m in self._mods:
            if m.file_id and m.file_id in info:
                opt, cj_name, policy = info[m.file_id]
                if bool(getattr(m, "optional", False)) != opt:
                    m.optional = opt
                    changed = True
                cur = m.mod_name or ""
                ambiguous = (not cur) or name_counts.get(cur, 1) > 1
                if cj_name and ambiguous and cur != cj_name:
                    m.mod_name = cj_name
                    changed = True
                if getattr(m, "update_policy", "exact") != policy:
                    m.update_policy = policy
                    changed = True
        return changed

    def _on_manifest_ready(self, payload):
        token, offsite, manifest = payload
        if token != self._detail_token:
            return                       # a newer revision switch superseded this
        if manifest:
            self._fetched_manifest = manifest
            self._fetched_manifest_rev = self._resolved_viewing_revision()
            self._recommend_new_profile = bool(
                (manifest.get("collectionConfig") or {}).get("recommendNewProfile", False))
        self._offsite = list(offsite or [])
        self._data_ready = not self._unsupported_collection_schema
        self.refresh_install_options()
        info = ((manifest.get("info") or {})
                if isinstance(manifest, dict) else {})
        if isinstance(info, dict):
            versions = [str(version or "").strip()
                        for version in (info.get("gameVersions") or [])
                        if str(version or "").strip()]
            if versions:
                self._game_versions = versions
        if manifest and self._apply_manifest_overrides(manifest):
            self._fill_table()
            self._fill_optional()
        if not offsite:
            self._offsite_wrap.setVisible(False)
            return
        p = active_palette()
        self._offsite_title.setText(
            self.tr("Off-site mods ({0}) - download manually:").format(len(offsite)))
        # Clear prior rows (keep the trailing stretch).
        while self._offsite_layout.count() > 1:
            it = self._offsite_layout.takeAt(0)
            w = it.widget()
            if w is not None:
                w.setParent(None)
                w.deleteLater()
        for i, (name, url) in enumerate(offsite):
            row = QWidget()
            rl = QHBoxLayout(row); rl.setContentsMargins(0, 0, 0, 0); rl.setSpacing(6)
            nl = QLabel(name or url)
            nl.setWordWrap(True)
            nl.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
            nl.setStyleSheet(f"color:{_c(p,'TEXT_MAIN')}; font-size:11px;")
            rl.addWidget(nl, 1)
            openb = QPushButton(self.tr("Open"))
            openb.setObjectName("FormButton")
            openb.setCursor(Qt.PointingHandCursor)
            openb.clicked.connect(lambda _=False, u=url: self._open_url(u))
            rl.addWidget(openb)
            self._offsite_layout.insertWidget(i, row)
        self._offsite_scroll.setFixedHeight(min(230, max(60, len(offsite) * 38)))
        self._offsite_wrap.setVisible(True)

    # -- actions ------------------------------------------------------------
    def _collection_url(self) -> str:
        return (f"https://www.nexusmods.com/games/{self._domain}/collections/"
                f"{getattr(self._collection, 'slug', '')}")

    def _open_on_nexus(self):
        self._open_url(self._collection_url())

    def _open_url(self, url):
        from Utils.environment.xdg import open_url
        open_url(url, log_fn=self._log)

    def set_install_handler(self, handler):
        """Set the ``handler(chosen_fids, skipped_fids)`` invoked by the Install
        button (the app wires the real automatic-install flow here)."""
        self._on_install = handler

    def optional_selection(self):
        """Return (chosen_fids, skipped_fids) from the optional checklist."""
        chosen = {fid for cb, fid in self._opt_boxes if cb.isChecked() and fid}
        skipped = {fid for cb, fid in self._opt_boxes if not cb.isChecked() and fid}
        return chosen, skipped

    def install_mods(self, skipped_fids):
        """Return the list of NexusCollectionMods to install: every mod except
        the unticked optionals.

        Thunderstore entries are excluded: they are installed by a separate
        pass before the orchestrator runs, which is keyed on the integer Nexus
        file ids a Thunderstore package has none of."""
        return [m for m in self._mods
                if getattr(m, "source_type", "") != "thunderstore"
                and not (getattr(m, "optional", False)
                         and m.file_id in skipped_fids)]

    def thunderstore_mods(self):
        """The manifest's Thunderstore entries (excluded from install_mods)."""
        return [m for m in self._mods
                if getattr(m, "source_type", "") == "thunderstore"]

    def skipped_optional_mods(self, skipped_fids):
        """The full mod objects for the unticked optionals - the orchestrator
        removes these from an existing profile on continue/append/update."""
        return [m for m in self._mods
                if getattr(m, "source_type", "") != "thunderstore"
                and getattr(m, "optional", False)
                and m.file_id in skipped_fids]

    @property
    def download_link_path(self):
        return self._dl_path

    @property
    def game_versions(self) -> tuple[str, ...]:
        return tuple(self._game_versions)

    def _on_install_clicked(self):
        chosen, skipped = self.optional_selection()
        intent = getattr(self, "_install_intent", "install")
        self._log(f"Collection {intent}: {len(chosen)} optional kept, "
                  f"{len(skipped)} skipped.")
        if self._on_install is not None:
            self._on_install(chosen, skipped, intent)
