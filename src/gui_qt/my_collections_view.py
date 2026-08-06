"""My Collections - manage the collections the signed-in user owns.

Lists everything ``myCollections`` returns (including unlisted collections and
unpublished drafts), lets the author edit the metadata Nexus exposes over the
API (name / summary / description / category), write a revision changelog,
publish a draft revision listed or unlisted, and toggle a published
collection's visibility.

Everything that touches the network runs on a worker thread and reports back
through signals; the API layer lives in ``Nexus.nexus_api``.
"""

from __future__ import annotations

import threading

from PySide6.QtCore import Qt, Signal, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, QPlainTextEdit,
    QPushButton, QComboBox, QTableWidget, QTableWidgetItem, QHeaderView,
    QAbstractItemView, QCheckBox, QFrame, QRadioButton, QButtonGroup,
)

from gui_qt.safe_emit import safe_emit
from gui_qt.theme_qt import active_palette, _c
from gui_qt.export_profile_view import _CardOverlay, _card_title, _card_button_bar

_COL_NAME, _COL_GAME, _COL_STATUS, _COL_REVISION, _COL_DOWNLOADS = range(5)

_STATUS_LABELS = {
    "listed": "Listed",
    "unlisted": "Unlisted",
    "under_moderation": "Under moderation",
    "discarded": "Discarded",
}


class _PublishOverlay(_CardOverlay):
    """Confirm publishing a draft revision: visibility + adult-content flag."""

    CARD_W = 500
    CARD_H = 340

    def __init__(self, host, collection_name, revision_number, mod_count,
                 currently_listed, on_publish):
        super().__init__(host)
        self._on_publish = on_publish
        self._body.addWidget(_card_title(
            self.tr("Publish revision {0}").format(revision_number)))
        sub = QLabel(self.tr(
            "'{0}' - {1} mod(s).\n\nPublishing makes this revision the one "
            "users install. It cannot be un-published, only retracted.").format(
                collection_name, mod_count))
        sub.setObjectName("CardSub")
        sub.setWordWrap(True)
        self._body.addWidget(sub)

        self._group = QButtonGroup(self)
        self._radios = {}
        for value, label, desc in (
            (True, self.tr("Listed"),
             self.tr("Public - appears in collection search")),
            (False, self.tr("Unlisted"),
             self.tr("Only reachable by direct link")),
        ):
            rb = QRadioButton(self.tr("{0}   - {1}").format(label, desc))
            rb.setChecked(value == currently_listed)
            self._group.addButton(rb)
            self._radios[value] = rb
            row = QFrame()
            row.setObjectName("CardOption")
            row_l = QVBoxLayout(row)
            row_l.setContentsMargins(0, 0, 0, 0)
            row_l.addWidget(rb)
            self._body.addWidget(row)

        self._adult = QCheckBox(self.tr("Contains adult content"))
        self._body.addWidget(self._adult)
        self._body.addStretch(1)
        self._body.addLayout(
            _card_button_bar(self, self.tr("Publish"), self._apply))
        self._show_over()

    def _apply(self):
        listed = self._radios[True].isChecked()
        adult = self._adult.isChecked()
        cb = self._on_publish
        self._finish()
        if cb:
            cb(listed, adult)

    def _cancel(self):
        self._finish()


class MyCollectionsView(QWidget):
    """Fullscreen tab: the user's own collections, with edit/publish actions."""

    _loaded = Signal(object, str)        # (list[MyCollection] | None, error)
    _action_done = Signal(bool, str)     # (ok, message)
    _categories_ready = Signal(object)   # [(id, name), …]

    def __init__(self, window, api, log_fn=None):
        super().__init__()
        self._window = window
        self._api = api
        self._log = log_fn or (lambda _m: None)
        self._collections: list = []
        self._selected: "object | None" = None
        self._categories: list = []
        self._busy = False

        self.setObjectName("MyCollectionsView")
        self._loaded.connect(self._on_loaded)
        self._action_done.connect(self._on_action_done)
        self._categories_ready.connect(self._on_categories_ready)
        self._build()
        self.refresh()

    # -- construction -------------------------------------------------------
    def _qss(self) -> str:
        p = active_palette()
        c = lambda k: _c(p, k)
        return f"""
        #MyCollectionsView {{ background: {c('BG_DEEP')}; }}
        #MCTitleBar {{ background: {c('BG_HEADER')};
                       border-bottom: 1px solid {c('BORDER')}; }}
        #MCTitle {{ color: {c('TEXT_MAIN')}; font-weight: 600; font-size: 15px; }}
        #MCSidePanel {{ background: {c('BG_HEADER')};
                        border-left: 1px solid {c('BORDER')}; }}
        #MCSidePanel QLabel {{ color: {c('TEXT_MAIN')}; }}
        #MCHint {{ color: {c('TEXT_DIM')}; }}
        QTableWidget {{ background: {c('BG_DEEP')}; color: {c('TEXT_MAIN')};
                        gridline-color: {c('BORDER')}; border: none; }}
        QHeaderView::section {{ background: {c('BG_HEADER')};
                        color: {c('TEXT_MAIN')}; border: none;
                        border-bottom: 1px solid {c('BORDER')}; padding: 4px; }}
        """

    def _build(self):
        self.setStyleSheet(self._qss())
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        bar = QWidget()
        bar.setObjectName("MCTitleBar")
        hb = QHBoxLayout(bar)
        hb.setContentsMargins(12, 8, 12, 8)
        title = QLabel(self.tr("My Collections"))
        title.setObjectName("MCTitle")
        hb.addWidget(title)
        hb.addStretch(1)
        refresh = QPushButton(self.tr("Refresh"))
        refresh.setObjectName("FormButton")
        refresh.setCursor(Qt.PointingHandCursor)
        refresh.clicked.connect(self.refresh)
        hb.addWidget(refresh)
        close = QPushButton(self.tr("✕ Close"))
        close.setObjectName("FormButton")
        close.setCursor(Qt.PointingHandCursor)
        close.clicked.connect(self._close)
        hb.addWidget(close)
        root.addWidget(bar)

        content = QWidget()
        ch = QHBoxLayout(content)
        ch.setContentsMargins(0, 0, 0, 0)
        ch.setSpacing(0)

        self._table = QTableWidget(0, 5)
        self._table.setHorizontalHeaderLabels(
            [self.tr("Collection"), self.tr("Game"), self.tr("Status"),
             self.tr("Revision"), self.tr("Downloads")])
        self._table.verticalHeader().setVisible(False)
        self._table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.SingleSelection)
        self._table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        hh = self._table.horizontalHeader()
        hh.setSectionResizeMode(_COL_NAME, QHeaderView.Stretch)
        for col in (_COL_GAME, _COL_STATUS, _COL_REVISION, _COL_DOWNLOADS):
            hh.setSectionResizeMode(col, QHeaderView.ResizeToContents)
        self._table.itemSelectionChanged.connect(self._on_selection_changed)
        ch.addWidget(self._table, 1)

        panel = QWidget()
        panel.setObjectName("MCSidePanel")
        panel.setFixedWidth(340)
        pv = QVBoxLayout(panel)
        pv.setContentsMargins(12, 10, 12, 10)
        pv.setSpacing(4)

        self._detail_title = QLabel(self.tr("Select a collection"))
        self._detail_title.setStyleSheet("font-weight: 600; font-size: 14px;")
        self._detail_title.setWordWrap(True)
        pv.addWidget(self._detail_title)
        self._detail_hint = QLabel("")
        self._detail_hint.setObjectName("MCHint")
        self._detail_hint.setWordWrap(True)
        pv.addWidget(self._detail_hint)
        pv.addSpacing(6)

        self._name = QLineEdit()
        self._summary = QLineEdit()
        self._summary.setPlaceholderText(self.tr("One-line summary"))
        self._description = QPlainTextEdit()
        self._description.setFixedHeight(90)
        self._category = QComboBox()
        for label, widget in ((self.tr("Name"), self._name),
                              (self.tr("Summary"), self._summary),
                              (self.tr("Category"), self._category),
                              (self.tr("Description"), self._description)):
            pv.addWidget(QLabel(label))
            pv.addWidget(widget)
            pv.addSpacing(2)

        self._save_btn = QPushButton(self.tr("Save changes"))
        self._save_btn.setObjectName("FormButton")
        self._save_btn.setCursor(Qt.PointingHandCursor)
        self._save_btn.clicked.connect(self._on_save_metadata)
        pv.addWidget(self._save_btn)
        pv.addSpacing(8)

        pv.addWidget(QLabel(self.tr("Revision changelog")))
        self._changelog = QPlainTextEdit()
        self._changelog.setFixedHeight(70)
        self._changelog.setPlaceholderText(
            self.tr("What changed in this revision - optional"))
        pv.addWidget(self._changelog)
        self._changelog_btn = QPushButton(self.tr("Save changelog"))
        self._changelog_btn.setObjectName("FormButton")
        self._changelog_btn.setCursor(Qt.PointingHandCursor)
        self._changelog_btn.clicked.connect(self._on_save_changelog)
        pv.addWidget(self._changelog_btn)
        pv.addStretch(1)

        self._publish_btn = QPushButton(self.tr("Publish draft revision"))
        self._publish_btn.setObjectName("PrimaryButton")
        self._publish_btn.setCursor(Qt.PointingHandCursor)
        self._publish_btn.clicked.connect(self._on_publish)
        pv.addWidget(self._publish_btn)

        self._install_btn = QPushButton(self.tr("Install collection"))
        self._install_btn.setObjectName("FormButton")
        self._install_btn.setCursor(Qt.PointingHandCursor)
        self._install_btn.clicked.connect(self._on_install)
        pv.addWidget(self._install_btn)

        row = QHBoxLayout()
        row.setSpacing(8)
        self._listing_btn = QPushButton(self.tr("Unlist"))
        self._listing_btn.setObjectName("FormButton")
        self._listing_btn.setCursor(Qt.PointingHandCursor)
        self._listing_btn.clicked.connect(self._on_toggle_listing)
        row.addWidget(self._listing_btn, 1)
        self._open_btn = QPushButton(self.tr("Open on Nexus"))
        self._open_btn.setObjectName("FormButton")
        self._open_btn.setCursor(Qt.PointingHandCursor)
        self._open_btn.clicked.connect(self._on_open_web)
        row.addWidget(self._open_btn, 1)
        pv.addLayout(row)

        ch.addWidget(panel)
        root.addWidget(content, 1)
        self._set_detail_enabled(False)

    # -- loading ------------------------------------------------------------
    def refresh(self):
        if self._api is None:
            self._detail_hint.setText(
                self.tr("Log in to Nexus to manage your collections."))
            return
        self._detail_hint.setText(self.tr("Loading…"))
        threading.Thread(target=self._load_worker, daemon=True,
                         name="my-collections-load").start()

    def _load_worker(self):
        try:
            collections = self._api.get_my_collections()
            safe_emit(self._loaded, collections, "")
        except Exception as exc:
            safe_emit(self._loaded, None, str(exc))
        if not self._categories:
            try:
                cats = self._api.get_collection_categories()
            except Exception:
                cats = []
            if cats:
                safe_emit(self._categories_ready, cats)

    def _on_categories_ready(self, cats):
        self._categories = list(cats)
        current = self._category.currentData()
        self._category.clear()
        for cid, name in self._categories:
            self._category.addItem(name, cid)
        if current:
            self._select_category(current)

    def _select_category(self, category_id: int):
        idx = self._category.findData(category_id)
        if idx >= 0:
            self._category.setCurrentIndex(idx)

    def _on_loaded(self, collections, error: str):
        if collections is None:
            self._detail_hint.setText(
                self.tr("Could not load collections: {0}").format(error))
            self._log(f"[my-collections] load failed: {error}")
            return
        self._collections = list(collections)
        if not self._collections:
            self._detail_hint.setText(self.tr(
                "You have no collections yet. Use Nexus ▸ Collections ▸ "
                "Create collection… to upload one."))
        # With rows present the selection handler owns the hint text - set it
        # BEFORE rebuilding so the row it auto-selects has the last word.
        self._rebuild_table()

    def _rebuild_table(self):
        keep_id = getattr(self._selected, "id", None)
        t = self._table
        t.blockSignals(True)
        t.setRowCount(0)
        t.setRowCount(len(self._collections))
        for i, col in enumerate(self._collections):
            def _cell(text):
                item = QTableWidgetItem(text)
                item.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
                return item

            t.setItem(i, _COL_NAME, _cell(col.name or col.slug))
            t.setItem(i, _COL_GAME, _cell(col.game_name or col.game_domain))
            t.setItem(i, _COL_STATUS,
                      _cell(self.tr(_STATUS_LABELS.get(col.status, col.status))))
            draft = col.draft_revision
            if draft is not None:
                rev = self.tr("{0} (draft)").format(draft.revision_number)
            elif col.latest_published_revision:
                rev = str(col.latest_published_revision)
            else:
                rev = "-"
            t.setItem(i, _COL_REVISION, _cell(rev))
            t.setItem(i, _COL_DOWNLOADS, _cell(f"{col.total_downloads:,}"))
        t.blockSignals(False)

        if keep_id is not None:
            for i, col in enumerate(self._collections):
                if col.id == keep_id:
                    t.selectRow(i)
                    return
        if self._collections:
            t.selectRow(0)
        else:
            self._selected = None
            self._set_detail_enabled(False)

    # -- selection ----------------------------------------------------------
    def _on_selection_changed(self):
        rows = self._table.selectionModel().selectedRows()
        if not rows:
            self._selected = None
            self._set_detail_enabled(False)
            return
        idx = rows[0].row()
        if not (0 <= idx < len(self._collections)):
            return
        col = self._collections[idx]
        self._selected = col
        self._name.setText(col.name)
        self._summary.setText(col.summary)
        self._description.setPlainText(col.description)
        if col.category_id:
            self._select_category(col.category_id)
        draft = col.draft_revision
        target = draft or (col.revisions[0] if col.revisions else None)
        self._changelog.setPlainText(target.changelog if target else "")
        self._set_detail_enabled(True)

        if draft is not None:
            self._detail_title.setText(col.name or col.slug)
            self._detail_hint.setText(self.tr(
                "Revision {0} is an unpublished draft ({1} mods). Publish it "
                "to make it the version users install.").format(
                    draft.revision_number, draft.mod_count))
            self._publish_btn.setEnabled(True)
            self._publish_btn.setText(
                self.tr("Publish revision {0}").format(draft.revision_number))
        else:
            self._detail_title.setText(col.name or col.slug)
            if col.latest_published_revision:
                self._detail_hint.setText(self.tr(
                    "Revision {0} is live. Upload a new revision from Create "
                    "Collection to publish an update.").format(
                        col.latest_published_revision))
            else:
                self._detail_hint.setText(self.tr("No revisions yet."))
            self._publish_btn.setEnabled(False)
            self._publish_btn.setText(self.tr("No draft to publish"))

        listed = col.status == "listed"
        self._listing_btn.setText(
            self.tr("Unlist") if listed else self.tr("List publicly"))
        # Nexus only accepts the listing toggle once something is published.
        self._listing_btn.setEnabled(bool(col.latest_published_revision))
        self._changelog_btn.setEnabled(target is not None)

        # Install pulls the published revision when there is one; an author can
        # also install their own draft to test it before publishing.
        install_rev = self._install_revision(col)
        if install_rev is None:
            self._install_btn.setEnabled(False)
            self._install_btn.setText(self.tr("Install collection"))
            self._install_btn.setToolTip(
                self.tr("This collection has no revisions yet."))
        else:
            self._install_btn.setEnabled(True)
            if draft is not None and install_rev == draft.revision_number:
                self._install_btn.setText(
                    self.tr("Install draft revision {0}").format(install_rev))
                self._install_btn.setToolTip(self.tr(
                    "Installs your unpublished draft so you can test it."))
            else:
                self._install_btn.setText(
                    self.tr("Install revision {0}").format(install_rev))
                self._install_btn.setToolTip(self.tr(
                    "Opens this collection's install page."))

    def _set_detail_enabled(self, on: bool):
        for w in (self._name, self._summary, self._description, self._category,
                  self._save_btn, self._changelog, self._changelog_btn,
                  self._publish_btn, self._install_btn, self._listing_btn,
                  self._open_btn):
            w.setEnabled(on)
        if not on:
            self._detail_title.setText(self.tr("Select a collection"))
            self._name.clear()
            self._summary.clear()
            self._description.clear()
            self._changelog.clear()

    # -- actions ------------------------------------------------------------
    def _run(self, fn, busy_message: str):
        """Run an API call off-thread with the panel disabled meanwhile."""
        if self._busy or self._selected is None:
            return
        self._busy = True
        self._set_detail_enabled(False)
        self._detail_hint.setText(busy_message)

        def _worker():
            try:
                ok, message = fn()
            except Exception as exc:
                ok, message = False, str(exc)
            safe_emit(self._action_done, ok, message)

        threading.Thread(target=_worker, daemon=True,
                         name="my-collections-action").start()

    def _on_action_done(self, ok: bool, message: str):
        self._busy = False
        self._set_detail_enabled(True)
        if ok:
            self._notify(message, "info")
            self._log(f"[my-collections] {message}")
            self.refresh()
        else:
            self._notify(self.tr("Failed: {0}").format(message), "error")
            self._log(f"[my-collections] failed: {message}")
            self._on_selection_changed()

    def _on_save_metadata(self):
        col = self._selected
        if col is None:
            return
        name = self._name.text().strip()
        from Utils.collection_export import validate_collection_name
        problem = validate_collection_name(name)
        if problem:
            self._notify(problem, "warning")
            return
        summary = self._summary.text().strip()
        description = self._description.toPlainText().strip()
        category_id = self._category.currentData() or 0
        changed = {}
        if name != col.name:
            changed["name"] = name
        if summary != col.summary:
            changed["summary"] = summary
        if description != col.description:
            changed["description"] = description
        if category_id and category_id != col.category_id:
            changed["category_id"] = category_id
        if not changed:
            self._notify(self.tr("Nothing to save."), "info")
            return

        def _call():
            ok = self._api.edit_collection(col.id, **changed)
            return ok, (self.tr("Saved '{0}'").format(name) if ok
                        else self.tr("Nexus rejected the edit (see log)."))

        self._run(_call, self.tr("Saving…"))

    def _on_save_changelog(self):
        col = self._selected
        if col is None:
            return
        target = col.draft_revision or (col.revisions[0] if col.revisions
                                        else None)
        if target is None:
            return
        text = self._changelog.toPlainText().strip()

        def _call():
            ok = self._api.set_revision_changelog(
                target.id, text, changelog_id=target.changelog_id)
            return ok, (self.tr("Changelog saved for revision {0}").format(
                target.revision_number) if ok
                else self.tr("Nexus rejected the changelog (see log)."))

        self._run(_call, self.tr("Saving changelog…"))

    def _on_publish(self):
        col = self._selected
        if col is None:
            return
        draft = col.draft_revision
        if draft is None:
            return

        def _confirmed(listed: bool, adult: bool):
            def _call():
                ok = self._api.publish_revision(draft.id, listed=listed,
                                                adult_content=adult)
                return ok, (self.tr("Published revision {0}").format(
                    draft.revision_number) if ok
                    else self.tr("Nexus rejected the publish (see log)."))

            self._run(_call, self.tr("Publishing…"))

        _PublishOverlay(self.window(), col.name or col.slug,
                        draft.revision_number, draft.mod_count,
                        col.status != "unlisted", _confirmed)

    def _on_toggle_listing(self):
        col = self._selected
        if col is None:
            return
        listed = col.status != "listed"

        def _call():
            ok = self._api.set_collection_listed(col.id, listed)
            return ok, ((self.tr("'{0}' is now listed") if listed
                         else self.tr("'{0}' is now unlisted")).format(col.name)
                        if ok else self.tr("Nexus rejected the change (see log)."))

        self._run(_call, self.tr("Updating visibility…"))

    @staticmethod
    def _install_revision(col) -> "int | None":
        """Revision to install: the newest published one, else the draft."""
        if col.latest_published_revision:
            return col.latest_published_revision
        draft = col.draft_revision
        if draft is not None:
            return draft.revision_number
        if col.revisions:
            return max(r.revision_number for r in col.revisions)
        return None

    def _on_install(self):
        """Hand the collection to the normal install pipeline (the same detail
        tab the collections browser and nxm:// links open)."""
        col = self._selected
        if col is None:
            return
        revision = self._install_revision(col)
        if revision is None:
            return

        window = self._window
        game = getattr(getattr(window, "_gs", None), "game", None)
        if game is None or not getattr(game, "is_configured", lambda: False)():
            self._notify(self.tr("No configured game selected."), "warning")
            return
        game_domain = (getattr(game, "nexus_game_domain", "") or "").strip()
        if (col.game_domain and game_domain
                and col.game_domain.lower() != game_domain.lower()):
            self._notify(self.tr(
                "'{0}' is for {1}, but the selected game is {2}. Switch games "
                "first, then install.").format(
                    col.name or col.slug, col.game_name or col.game_domain,
                    getattr(game, "name", game_domain)), "warning")
            return

        opener = getattr(window, "_open_collection_detail_tab", None)
        if not callable(opener):
            self._notify(self.tr("Install is unavailable in this build."),
                         "warning")
            return

        from Nexus.nexus_api import NexusCollection
        rev = next((r for r in col.revisions
                    if r.revision_number == revision), None)
        opener(NexusCollection(
            id=col.id, slug=col.slug, name=col.name or col.slug,
            summary=col.summary, user_name="",
            total_downloads=col.total_downloads,
            endorsements=col.endorsements,
            mod_count=(rev.mod_count if rev else 0),
            game_domain=col.game_domain,
        ), revision_number=revision)

    def _on_open_web(self):
        col = self._selected
        if col is None:
            return
        url = col.url()
        if url:
            QDesktopServices.openUrl(QUrl(url))

    # -- misc ---------------------------------------------------------------
    def _close(self):
        tabs = getattr(self._window, "_tabs", None)
        if tabs is not None:
            try:
                tabs.close_tab("my_collections")
                if getattr(self._window, "_my_collections_view", None) is self:
                    self._window._my_collections_view = None
                return
            except Exception:
                pass
        self.hide()

    def _notify(self, text: str, state: str = "info"):
        n = getattr(self._window, "_notify", None)
        if callable(n):
            n(text, state)
