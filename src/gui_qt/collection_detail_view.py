
from __future__ import annotations

import threading

from PySide6.QtCore import Qt, Signal, QT_TRANSLATE_NOOP
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QFrame,
    QSplitter, QScrollArea, QCheckBox, QComboBox, QTableWidget, QTableWidgetItem,
    QHeaderView, QAbstractItemView,
)

from gui_qt.theme_qt import active_palette, _c
from gui_qt.safe_emit import safe_emit
from gui_qt.worker import run_in_worker
from Utils.collection_manifest import fmt_size


class _SizeItem(QTableWidgetItem):
    """Size cell: shows the humanized string (DisplayRole only) but sorts by the
    raw byte count stashed in UserRole. Setting EditRole to an int made the view
    render the raw number instead of the formatted text, so keep it off the
    item and compare via UserRole here."""

    def __lt__(self, other):
        try:
            return (self.data(Qt.UserRole) or 0) < (other.data(Qt.UserRole) or 0)
        except Exception:
            return super().__lt__(other)


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
        self._revision_number = revision_number    # None = latest published
        # A ctor-requested revision (e.g. Open Current) - the FIRST fetch is still
        # done at "latest" so the revisions list + dropdown populate, then we
        # switch to this one.
        self._pending_initial_rev = revision_number
        self._revisions_list: list[dict] = []
        self._detail_token = 0                     # guards stale revision fetches

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
                category_id=int(cat.get("id") or 0),
                category_name=(cat.get("name") or "").strip(),
                domain_name=(m.get("domainName") or "").strip()))
        self._mods = mods
        self._total_size = int(total_size or 0)
        self._size_lbl.setText(
            self.tr("Total size: {0}  |  {1} mods").format(fmt_size(total_size), len(mods)))
        self._fill_table()
        self._fill_optional()
        # Optional flags already came straight from the manifest - no override.
        self._on_manifest_ready((self._detail_token, offsite, None))

    # -- construction -------------------------------------------------------
    def _build(self):
        p = active_palette()
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # Header.
        bar = QWidget(); bar.setObjectName("HeaderBar")
        hb = QHBoxLayout(bar); hb.setContentsMargins(12, 8, 12, 8); hb.setSpacing(10)
        col = self._collection
        title = QLabel(col.name or col.slug or "Collection")
        title.setStyleSheet(
            f"color:{_c(p,'TEXT_MAIN')}; font-weight:600; font-size:15px;")
        self._title_lbl = title
        hb.addWidget(title)
        if col.user_name:
            author = QLabel(self.tr("by {0}").format(col.user_name))
            author.setStyleSheet(f"color:{_c(p,'TEXT_DIM')}; font-size:12px;")
            hb.addWidget(author)
        summ = (col.summary or "").strip()
        if summ:
            s = QLabel(summ)
            s.setStyleSheet(f"color:{_c(p,'TEXT_DIM')}; font-size:12px;")
            s.setMaximumWidth(520)
            hb.addWidget(s)
        hb.addStretch(1)
        # A combo (not SelectorButton, which builds a giant flat menu): its popup
        # scrolls + is hard height-capped (see _RevisionCombo), so hundreds of
        # revisions never open a full-screen list.
        self._rev_selector = _RevisionCombo()
        self._rev_selector.setMinimumWidth(150)
        self._rev_selector.setMaxVisibleItems(14)
        self._rev_selector.view().setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self._rev_selector.setVisible(False)      # shown once revisions arrive
        # Guard so programmatic set_items/setCurrentIndex don't fire the handler.
        self._rev_updating = False
        self._rev_selector.currentIndexChanged.connect(self._on_revision_index)
        hb.addWidget(self._rev_selector)
        self._size_lbl = QLabel(self.tr("Loading…"))
        self._size_lbl.setStyleSheet(f"color:{_c(p,'TEXT_DIM')}; font-size:12px;")
        hb.addWidget(self._size_lbl)
        root.addWidget(bar)

        # Body: left (table / off-site - vertically resizable) | right (optional /
        # actions).
        body = QSplitter(Qt.Horizontal)

        # LEFT column is a VERTICAL splitter so the off-site section can be
        # resized against the mod table above it.
        left = QSplitter(Qt.Vertical)

        table_wrap = QWidget()
        lv = QVBoxLayout(table_wrap)
        lv.setContentsMargins(8, 8, 8, 4); lv.setSpacing(6)

        # (red) sortable mod table.
        self._table = QTableWidget(0, len(_COLS))
        self._table.setHorizontalHeaderLabels([self.tr(c) for c in _COLS])
        self._table.setSortingEnabled(True)
        self._table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self._table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.SingleSelection)
        self._table.verticalHeader().setVisible(False)
        self._table.setAlternatingRowColors(True)
        hh = self._table.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.Stretch)          # Name fills
        for c in range(1, len(_COLS)):
            hh.setSectionResizeMode(c, QHeaderView.Interactive)
        # QTableView isn't covered by the global QTreeView/QListView list QSS.
        self._table.setStyleSheet(
            f"QTableWidget {{ background:{_c(p,'BG_LIST')};"
            f" alternate-background-color:{_c(p,'BG_ROW_ALT')};"
            f" color:{_c(p,'TEXT_MAIN')}; border:1px solid {_c(p,'BORDER')};"
            f" gridline-color:{_c(p,'BORDER')}; }}"
            f"QTableWidget::item:selected {{ background:{_c(p,'BG_SELECT')};"
            f" color:{_c(p,'TEXT_ON_ACCENT')}; }}")
        lv.addWidget(self._table, 1)
        left.addWidget(table_wrap)

        # (yellow) off-site section - hidden until the manifest lands. A separate
        # splitter pane so the divider above it can resize the two vertically.
        self._offsite_panel = QFrame()
        self._offsite_panel.setObjectName("OffsitePanel")
        self._offsite_panel.setStyleSheet(
            f"#OffsitePanel {{ background:{_c(p,'BG_PANEL')};"
            f" border:1px solid {_c(p,'BORDER')}; border-radius:4px; }}")
        ov = QVBoxLayout(self._offsite_panel)
        ov.setContentsMargins(8, 6, 8, 6); ov.setSpacing(3)
        self._offsite_title = QLabel(self.tr("Off-site mods"))
        self._offsite_title.setStyleSheet(
            f"color:{_c(p,'TEXT_WARN')}; font-weight:600; font-size:12px;")
        ov.addWidget(self._offsite_title)
        self._offsite_scroll = QScrollArea()
        self._offsite_scroll.setWidgetResizable(True)
        self._offsite_scroll.setFrameShape(QFrame.NoFrame)
        self._offsite_host = QWidget()
        self._offsite_layout = QVBoxLayout(self._offsite_host)
        self._offsite_layout.setContentsMargins(0, 0, 0, 0)
        self._offsite_layout.setSpacing(2)
        self._offsite_layout.addStretch(1)
        self._offsite_scroll.setWidget(self._offsite_host)
        ov.addWidget(self._offsite_scroll, 1)
        # A small wrapper with margins so the splitter handle has breathing room.
        offsite_wrap = QWidget()
        owrap = QVBoxLayout(offsite_wrap)
        owrap.setContentsMargins(8, 4, 8, 8); owrap.setSpacing(0)
        owrap.addWidget(self._offsite_panel)
        left.addWidget(offsite_wrap)
        self._offsite_wrap = offsite_wrap
        self._offsite_wrap.setVisible(False)      # shown with the panel

        left.setStretchFactor(0, 4)               # table gets most of the height
        left.setStretchFactor(1, 1)
        left.setCollapsible(0, False)
        body.addWidget(left)

        # RIGHT column = optional-mods panel (green) + a SEPARATE actions panel.
        right = QWidget()
        rv = QVBoxLayout(right); rv.setContentsMargins(8, 8, 8, 8); rv.setSpacing(8)

        # --- (green) optional-mods panel: title + checklist + select-all row ---
        opt_panel = QFrame()
        opt_panel.setObjectName("OptPanel")
        opt_panel.setStyleSheet(
            f"#OptPanel {{ background:{_c(p,'BG_PANEL')};"
            f" border:1px solid {_c(p,'BORDER')}; border-radius:4px; }}")
        opv = QVBoxLayout(opt_panel)
        opv.setContentsMargins(8, 6, 8, 6); opv.setSpacing(6)
        opt_title = QLabel(self.tr("Optional mods"))
        opt_title.setStyleSheet(
            f"color:{_c(p,'TEXT_MAIN')}; font-weight:600; font-size:13px;")
        opv.addWidget(opt_title)
        self._opt_scroll = QScrollArea()
        self._opt_scroll.setWidgetResizable(True)
        self._opt_scroll.setFrameShape(QFrame.NoFrame)
        self._opt_host = QWidget()
        self._opt_layout = QVBoxLayout(self._opt_host)
        self._opt_layout.setContentsMargins(2, 2, 2, 2)
        self._opt_layout.setSpacing(4)
        self._opt_empty = QLabel(self.tr("Loading…"))
        self._opt_empty.setStyleSheet(f"color:{_c(p,'TEXT_DIM')};")
        self._opt_layout.addWidget(self._opt_empty)
        self._opt_layout.addStretch(1)
        self._opt_scroll.setWidget(self._opt_host)
        opv.addWidget(self._opt_scroll, 1)
        # Select all / Deselect all at the bottom of the optional-mods panel.
        selrow = QHBoxLayout(); selrow.setSpacing(6)
        self._select_all_btn = QPushButton(self.tr("Select all"))
        self._select_all_btn.setObjectName("FormButton")
        self._select_all_btn.setCursor(Qt.PointingHandCursor)
        self._select_all_btn.clicked.connect(lambda: self._set_all_optional(True))
        selrow.addWidget(self._select_all_btn)
        self._deselect_all_btn = QPushButton(self.tr("Deselect all"))
        self._deselect_all_btn.setObjectName("FormButton")
        self._deselect_all_btn.setCursor(Qt.PointingHandCursor)
        self._deselect_all_btn.clicked.connect(lambda: self._set_all_optional(False))
        selrow.addWidget(self._deselect_all_btn)
        selrow.addStretch(1)
        opv.addLayout(selrow)
        rv.addWidget(opt_panel, 1)

        # --- separate actions area (Install / View on Nexus) ------------------
        actions = QFrame()
        actions.setObjectName("ActionPanel")
        actions.setStyleSheet(
            f"#ActionPanel {{ background:{_c(p,'BG_HEADER')};"
            f" border:1px solid {_c(p,'BORDER')}; border-radius:4px; }}")
        av = QHBoxLayout(actions); av.setContentsMargins(8, 8, 8, 8); av.setSpacing(6)
        install = QPushButton(self.tr("Install collection"))
        install.setObjectName("PrimaryButton")
        install.setCursor(Qt.PointingHandCursor)
        install.clicked.connect(self._on_install_clicked)
        av.addWidget(install)
        self._install_btn = install
        self._install_intent = "install"     # install | update | resume
        view = QPushButton(self.tr("View on Nexus"))
        view.setObjectName("FormButton")
        view.setCursor(Qt.PointingHandCursor)
        view.clicked.connect(self._open_on_nexus)
        av.addWidget(view)
        av.addStretch(1)
        rv.addWidget(actions)

        body.addWidget(right)
        body.setStretchFactor(0, 3)     # mod list wider
        body.setStretchFactor(1, 2)
        root.addWidget(body, 1)

    # -- fetch: detail ------------------------------------------------------
    def _start_detail_fetch(self):
        self._detail_token += 1
        token = self._detail_token
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
                self._table.setRowCount(0)
                self._size_lbl.setText(self.tr("Loading…"))
                self._start_detail_fetch()
                return
            self._revision_number = want
        self._mods = list(mods or [])
        self._total_size = int(total_size or 0)
        self._size_lbl.setText(
            self.tr("Total size: {0}  |  {1} mods").format(fmt_size(total_size), mod_count))
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
            from Utils.game_helpers import find_profile_with_collection_slug
            from Utils.profile_state import read_collection_revision
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
            from Utils.game_helpers import find_profile_with_collection_slug
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
            from Utils.profile_state import read_collection_install_paused
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
        from Utils.ui_config import load_download_only
        try:
            dl_only = bool(load_download_only())
        except Exception:
            dl_only = False
        if dl_only:
            # Resume/update both need a profile a download-only run never creates.
            self._install_intent = "install"
            btn.setText(self.tr("Download collection"))
            return
        if self._is_paused():
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
        self._update_install_btn_state()

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
        self._table.setRowCount(0)
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
        self._table.setSortingEnabled(False)
        self._table.setRowCount(len(self._mods))
        for r, m in enumerate(self._mods):
            self._set_cell(r, 0, self._display_name(m))
            self._set_cell(r, 1, m.mod_author or "")
            self._set_cell(r, 2, m.version or "")
            # Size - humanized text, numeric sort via the raw bytes in UserRole.
            size_item = _SizeItem(fmt_size(m.size_bytes))
            size_item.setData(Qt.UserRole, int(m.size_bytes or 0))
            self._table.setItem(r, _COL_SIZE, size_item)
            self._set_cell(r, 4, "✓" if m.optional else "")
        self._table.setSortingEnabled(True)
        self._table.sortItems(0, Qt.AscendingOrder)

    def _set_cell(self, row, col, text):
        self._table.setItem(row, col, QTableWidgetItem(text))

    def _fill_optional(self):
        # In-session choices: keep the user's unticks when the checklist is
        # rebuilt (revision switch, manifest-override refresh).
        prior_fids = {fid for _cb, fid in self._opt_boxes if fid}
        prior_unticked = {fid for cb, fid in self._opt_boxes
                          if fid and not cb.isChecked()}
        # Clear the placeholder + any prior boxes.
        while self._opt_layout.count() > 1:      # keep the trailing stretch
            it = self._opt_layout.takeAt(0)
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
        if not has_opt:
            lbl = QLabel(self.tr("No optional mods."))
            lbl.setStyleSheet(f"color:{_c(active_palette(),'TEXT_DIM')};")
            self._opt_layout.insertWidget(0, lbl)
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
            self._opt_layout.insertWidget(i, cb)
            self._opt_boxes.append((cb, m.file_id))

    def _saved_skipped_fids(self) -> "set[int]":
        """Optional mods unticked on the LAST install of this collection, read
        from the profile that holds it. Empty set when none is saved."""
        _pname, pdir = self._collection_profile()
        if pdir is None or not pdir.is_dir():
            return set()
        try:
            from Utils.profile_state import read_collection_optional_skipped
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
                from Utils.collection_manifest import (
                    load_collection_manifest, extract_offsite_mods)
                manifest = load_collection_manifest(
                    self._api, game_name, slug, rev, dl_path, log_fn=self._log)
                offsite = extract_offsite_mods(manifest)
                if manifest:
                    # Keep for the install worker (Tk _collection_schema_cache
                    # parity) so install never needs a second manifest download.
                    self._fetched_manifest = manifest
                    self._fetched_manifest_rev = rev
                # Manifest rule: some collections must be installed as a NEW
                # profile (collectionConfig.recommendNewProfile). Capture it so
                # the install mode overlay can disable "Append".
                try:
                    self._recommend_new_profile = bool(
                        (manifest.get("collectionConfig") or {}).get(
                            "recommendNewProfile", False))
                except Exception:
                    pass
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
        info: "dict[int, tuple[bool, str]]" = {}   # file_id → (optional, name)
        for cm in (manifest or {}).get("mods", []):
            src = cm.get("source") or {}
            fid = src.get("fileId")
            if fid is not None:
                cj_name = (cm.get("name") or src.get("logicalFilename") or "")
                info[int(fid)] = (bool(cm.get("optional", False)), cj_name)
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
                opt, cj_name = info[m.file_id]
                if bool(getattr(m, "optional", False)) != opt:
                    m.optional = opt
                    changed = True
                cur = m.mod_name or ""
                ambiguous = (not cur) or name_counts.get(cur, 1) > 1
                if cj_name and ambiguous and cur != cj_name:
                    m.mod_name = cj_name
                    changed = True
        return changed

    def _on_manifest_ready(self, payload):
        token, offsite, manifest = payload
        if token != self._detail_token:
            return                       # a newer revision switch superseded this
        self._offsite = list(offsite or [])
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
            nl.setStyleSheet(f"color:{_c(p,'TEXT_MAIN')}; font-size:11px;")
            rl.addWidget(nl, 1)
            openb = QPushButton(self.tr("Open"))
            openb.setObjectName("FormButton")
            openb.setCursor(Qt.PointingHandCursor)
            openb.clicked.connect(lambda _=False, u=url: self._open_url(u))
            rl.addWidget(openb)
            self._offsite_layout.insertWidget(i, row)
        self._offsite_wrap.setVisible(True)

    # -- actions ------------------------------------------------------------
    def _collection_url(self) -> str:
        return (f"https://www.nexusmods.com/games/{self._domain}/collections/"
                f"{getattr(self._collection, 'slug', '')}")

    def _open_on_nexus(self):
        self._open_url(self._collection_url())

    def _open_url(self, url):
        from Utils.xdg import open_url
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

    def _on_install_clicked(self):
        chosen, skipped = self.optional_selection()
        intent = getattr(self, "_install_intent", "install")
        self._log(f"Collection {intent}: {len(chosen)} optional kept, "
                  f"{len(skipped)} skipped.")
        if self._on_install is not None:
            self._on_install(chosen, skipped, intent)
