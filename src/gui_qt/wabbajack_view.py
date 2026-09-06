from __future__ import annotations

import copy
import queue
import threading
import uuid
from pathlib import Path

from PySide6.QtCore import Qt, Signal, QSize
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QLineEdit,
    QComboBox, QCheckBox, QListWidget, QListWidgetItem, QStackedWidget,
    QFormLayout, QPlainTextEdit, QTableWidget, QTableWidgetItem, QHeaderView, QScrollArea,
)

from gui_qt.safe_emit import safe_emit
from Utils.downloads.install import InstallCallbacks, InstallControl


class WabbajackView(QWidget):
    _result = Signal(str, object, str)
    _progress = Signal(str, object)
    _manual = Signal(object)
    _conflicts = Signal(object)
    installed = Signal(object, object)
    running_changed = Signal(bool)

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
        self._package_url = ""
        self._manual_package = False
        self._request = None
        self._report = None
        self._preflight_stop = threading.Event()
        self._page = 0
        self._busy = False
        self._control = InstallControl()
        self._answers = queue.Queue()
        self._overlay = None
        self._manual_overlay = None
        self._manual_row = None
        self._tokens = {}
        self._thumb_ids = {}
        self._thumb_sequence = 0
        self._result.connect(self._received)
        self._progress.connect(self._on_progress)
        self._manual.connect(self._on_manual)
        self._conflicts.connect(self._review_conflicts)
        self._build()
        from gui_qt.nexus_mod_card import ThumbnailLoader
        self._thumbs = ThumbnailLoader(self, crop_w=120, crop_h=80)
        self._thumbs.loaded.connect(self._thumbnail)
        self._refresh_installed()
        self._load_gallery()

    def _button(self, text, callback, layout):
        button = QPushButton(self.tr(text), self)
        button.setObjectName("FormButton")
        button.clicked.connect(callback)
        layout.addWidget(button)
        return button

    def _build(self):
        outer = QVBoxLayout(self)
        toolbar = QHBoxLayout()
        toolbar.addWidget(QLabel(self.tr("Wabbajack"), self))
        toolbar.addStretch()
        self._button("Open .wabbajack…", self._open_file, toolbar)
        self._button("Open URL…", self._open_url, toolbar)
        self._button("Refresh", lambda: self._load_gallery(True), toolbar)
        outer.addLayout(toolbar)
        self._status = QLabel(self)
        self._status.setWordWrap(True)
        outer.addWidget(self._status)
        self._stack = QStackedWidget(self)
        outer.addWidget(self._stack, 1)
        browser = QWidget(self)
        layout = QVBoxLayout(browser)
        filters = QHBoxLayout()
        self._search = QLineEdit(self)
        self._search.setPlaceholderText(self.tr("Search modlists…"))
        filters.addWidget(self._search, 1)
        self._game_filter = QComboBox(self)
        self._game_filter.addItem(self.tr("Current game"), "current")
        self._game_filter.addItem(self.tr("All games"), "")
        if self._game is None:
            self._game_filter.setCurrentIndex(1)
        filters.addWidget(self._game_filter)
        self._tag = QComboBox(self)
        self._tag.addItem(self.tr("All tags"), "")
        filters.addWidget(self._tag)
        self._adult = QCheckBox(self.tr("Show adult"), self)
        self._only_installed = QCheckBox(self.tr("Installed"), self)
        filters.addWidget(self._adult)
        filters.addWidget(self._only_installed)
        layout.addLayout(filters)
        self._list = QListWidget(self)
        self._list.setIconSize(QSize(120, 80))
        self._list.itemActivated.connect(self._open_entry)
        self._list.itemClicked.connect(self._open_entry)
        layout.addWidget(self._list, 1)
        footer = QHBoxLayout()
        self._button("Previous", lambda: self._turn_page(-1), footer)
        self._page_label = QLabel(self)
        footer.addWidget(self._page_label, 1)
        self._button("Next", lambda: self._turn_page(1), footer)
        layout.addLayout(footer)
        self._stack.addWidget(browser)
        for widget in (self._game_filter, self._tag):
            widget.currentIndexChanged.connect(self._filter_changed)
        for widget in (self._adult, self._only_installed):
            widget.toggled.connect(self._filter_changed)
        self._search.textChanged.connect(self._filter_changed)

        detail = QWidget(self)
        layout = QVBoxLayout(detail)
        bar = QHBoxLayout()
        self._button("Back to browser", lambda: self._stack.setCurrentIndex(0), bar)
        self._title = QLabel(self)
        self._title.setWordWrap(True)
        bar.addWidget(self._title, 1)
        self._detail_image = QLabel(self)
        bar.addWidget(self._detail_image)
        layout.addLayout(bar)
        self._description = QPlainTextEdit(self)
        self._description.setReadOnly(True)
        self._description.setMaximumHeight(160)
        layout.addWidget(self._description)
        links = QHBoxLayout()
        self._button("Author instructions", lambda: self._open_link("readme"), links)
        self._button("Community", lambda: self._open_link("community"), links)
        self._prepare_button = self._button("Download list / Set up", self._prepare_entry, links)
        layout.addLayout(links)
        form = QFormLayout()
        self._setup_form = form
        self._target_game = QComboBox(self)
        from Utils.wabbajack.games import configured_games
        self._games = configured_games()
        for name in self._games:
            self._target_game.addItem(name)
        if self._game:
            self._target_game.setCurrentText(self._game.name)
        form.addRow(self.tr("Target game"), self._target_game)
        self._directory = QLineEdit(self)
        form.addRow(self.tr("Managed installation"), self._directory)
        downloads = QHBoxLayout()
        self._downloads = QLineEdit(self)
        downloads.addWidget(self._downloads)
        self._button("Browse…", self._choose_downloads, downloads)
        form.addRow(self.tr("Downloads"), downloads)
        self._mode = QComboBox(self)
        self._mode.addItem(self.tr("New installation"), "install")
        self._mode.addItem(self.tr("Resume"), "resume")
        self._mode.addItem(self.tr("Repair"), "repair")
        self._mode.addItem(self.tr("Update"), "update")
        form.addRow(self.tr("Operation"), self._mode)
        self._profiles = QListWidget(self)
        self._profiles.setMaximumHeight(110)
        form.addRow(self.tr("Profiles"), self._profiles)
        self._adjustments = QListWidget(self)
        self._adjustments.setMaximumHeight(100)
        form.addRow(self.tr("Linux adjustments"), self._adjustments)
        self._adjustments.itemChanged.connect(self._invalidate)
        layout.addLayout(form)
        note = QLabel(self.tr("Profiles share mod files. File edits and list updates affect every linked profile; INIs and load orders remain separate."), self)
        note.setWordWrap(True)
        layout.addWidget(note)
        actions = QHBoxLayout()
        self._button("Install Texture Tool", self._install_texconv, actions)
        self._preflight_button = self._button("Check requirements", self._check, actions)
        self._start_button = self._button("Install", self._start, actions)
        self._start_button.setEnabled(False)
        layout.addLayout(actions)
        self._checks = QPlainTextEdit(self)
        self._checks.setReadOnly(True)
        layout.addWidget(self._checks, 1)
        detail_scroll = QScrollArea(self)
        detail_scroll.setWidgetResizable(True)
        detail_scroll.setFrameShape(QScrollArea.NoFrame)
        detail_scroll.setWidget(detail)
        self._stack.addWidget(detail_scroll)
        for widget in (self._directory, self._downloads):
            widget.textChanged.connect(self._invalidate)
        self._target_game.currentTextChanged.connect(self._game_selected)
        self._mode.currentIndexChanged.connect(self._invalidate)
        self._profiles.itemChanged.connect(self._invalidate)

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
        token = self._tokens.get(kind, 0) + 1
        self._tokens[kind] = token
        def run():
            try:
                result = work()
                safe_emit(self._result, kind, (token, result), "")
            except Exception as exc:
                safe_emit(self._result, kind, (token, None), str(exc))
        threading.Thread(target=run, daemon=True, name=f"wabbajack-{kind}").start()

    def _load_gallery(self, refresh=False):
        if self._busy:
            return
        self._status.setText(self.tr("Loading modlists…"))
        from Utils.wabbajack.gallery import load_gallery
        self._worker("gallery", lambda: load_gallery(refresh=refresh))

    def _refresh_installed(self):
        from Utils.wabbajack.store import installations
        self._installed = [info for game in self._games.values()
                           for info in installations(Path(game.get_profile_root()))]

    def _filter_changed(self, *_):
        self._page = 0
        self._render()

    def _turn_page(self, delta):
        self._page = max(0, self._page + delta)
        self._render()

    def _render(self):
        from Utils.wabbajack.games import matches_game
        query = self._search.text().casefold().strip()
        tag = self._tag.currentData()
        game = self._game_filter.currentData()
        installed = {i.get("gallery_id"): i for i in self._installed if i.get("gallery_id")}
        candidates = [(e, installed.get(e.id)) for e in self._entries]
        if self._only_installed.isChecked():
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
        rows = [(e, info) for e, info in candidates if (self._adult.isChecked() or not e.nsfw)
                and (not query or query in (e.title + " " + e.author + " " + e.description).casefold())
                and (not tag or tag in e.tags)
                and (not game or (game == "current" and self._game and matches_game(self._game, e.game)) or e.game == game)]
        self._page = min(self._page, max(0, (len(rows) - 1) // 20))
        self._list.clear()
        self._thumb_ids.clear()
        for entry, info in rows[self._page * 20:self._page * 20 + 20]:
            badges = ["Featured"] if entry.featured else []
            if entry.unavailable:
                badges.append("Unavailable")
            if info:
                badges.append("Update available" if self._has_update(entry, info) else "Installed")
                if self._only_installed.isChecked():
                    badges.append(info["id"][:8])
            item = QListWidgetItem(f"{entry.title}  {entry.version}\n{entry.author} · {entry.game}\n{' · '.join(badges)}", self._list)
            item.setData(Qt.UserRole, entry)
            item.setData(Qt.UserRole + 1, info)
            item.setToolTip(entry.description)
            self._thumb_sequence += 1
            self._thumb_ids[self._thumb_sequence] = item
            if entry.image:
                self._thumbs.request(self._thumb_sequence, entry.image)
        self._page_label.setText(self.tr("Page {0} · {1} lists").format(self._page + 1, len(rows)))

    def _thumbnail(self, index, pixmap):
        item = self._thumb_ids.get(index)
        if item:
            item.setIcon(QIcon(pixmap))
            if self._entry and item.data(Qt.UserRole).id == self._entry.id:
                self._detail_image.setPixmap(pixmap)

    @staticmethod
    def _has_update(entry, info):
        if entry.package_hash and info.get("package_xxhash"):
            from Utils.wabbajack.hashes import canonical_hash
            try:
                return canonical_hash(entry.package_hash) != info["package_xxhash"]
            except ValueError:
                pass
        return bool(entry.version and entry.version != info.get("version"))

    def _open_entry(self, item):
        if self._busy:
            return
        self._tokens["package"] = self._tokens.get("package", 0) + 1
        self._manual_package = False
        self._prepare_button.setText(self.tr("Download list / Set up"))
        self._entry = item.data(Qt.UserRole)
        self._info = item.data(Qt.UserRole + 1)
        self._detail_image.setPixmap(item.icon().pixmap(QSize(120, 80)))
        self._title.setText(f"{self._entry.title} {self._entry.version}")
        self._description.setPlainText(self._entry.description + "\n\n" + self.tr("Author: {0} · Downloads: {1:.1f} GiB · Installed: {2:.1f} GiB").format(
            self._entry.author, self._entry.download_size / 1024 ** 3, self._entry.install_size / 1024 ** 3))
        self._package = None
        self._invalidate()
        self._prepare_button.setEnabled(not self._entry.unavailable or bool(self._info))
        self._stack.setCurrentIndex(1)
        if self._info:
            self._directory.setText(self._info["directory"])
            self._downloads.setText(self._info.get("downloads", ""))
            mode = "resume" if self._info.get("status") != "complete" else (
                "update" if self._has_update(self._entry, self._info) else "repair")
            self._mode.setCurrentIndex(self._mode.findData(mode))
        else:
            self._game_selected()

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

    def _open_file(self):
        if self._busy:
            return
        from gui_qt.file_pickers import _pick_file
        path = _pick_file(self, self.tr("Open Wabbajack modlist"), [("Wabbajack modlists", ["*.wabbajack"])])
        if path:
            if self._stack.currentIndex() != 1 or not self._info:
                self._entry = self._info = None
            self._inspect(path)

    def _open_url(self):
        if self._busy:
            return
        from gui_qt.text_input_overlay import TextInputOverlay
        def accepted(value):
            if value and value.startswith(("https://", "http://")):
                self._entry = self._info = None
                self._download_package(value)
        TextInputOverlay.show_over(self, self.tr("Open Wabbajack URL"), self.tr("Direct .wabbajack URL:"), on_done=accepted)

    def _prepare_entry(self):
        if self._busy:
            return
        if self._manual_package:
            from PySide6.QtGui import QDesktopServices
            from PySide6.QtCore import QUrl
            QDesktopServices.openUrl(QUrl(self._package_url))
            return
        if not self._entry:
            return
        if self._info and self._mode.currentData() != "update":
            self._inspect(Path(self._info["package_path"]))
        elif self._entry.download:
            self._download_package(self._entry.download)
        else:
            self._status.setText(self.tr("This entry has no package download URL. Open a local .wabbajack file."))

    def _download_package(self, url):
        self._package_url = url
        self._manual_package = False
        self._package = None
        self._invalidate()
        from Utils.wabbajack.acquire import download_package
        from Utils.wabbajack.gallery import cache_root
        from Utils.wabbajack.manifest import inspect_package
        import hashlib
        path = cache_root() / (hashlib.sha256(url.encode()).hexdigest() + ".wabbajack")
        entry = self._entry
        self._status.setText(self.tr("Downloading modlist package…"))
        def work():
            download_package(url, path, size=entry.package_size if entry else 0,
                          expected=entry.package_hash if entry else "")
            return inspect_package(path)
        self._worker("package", work)

    def _inspect(self, path):
        self._package_url = ""
        self._manual_package = False
        self._prepare_button.setText(self.tr("Download list / Set up"))
        self._package = None
        self._invalidate()
        from Utils.wabbajack.manifest import inspect_package
        self._worker("package", lambda: inspect_package(path))

    def _game_selected(self, *_):
        game = self._games.get(self._target_game.currentText())
        if game and not self._info:
            from Utils.config_paths import get_download_cache_dir_for_game
            self._directory.setText(str(Path(game.get_profile_root()) / ".wabbajack" / uuid.uuid4().hex))
            self._downloads.setText(str(get_download_cache_dir_for_game(game.name)))
        self._invalidate()

    def _choose_downloads(self):
        from gui_qt.file_pickers import _pick_folder
        selected = _pick_folder(self, self.tr("Download directory"))
        if selected:
            self._downloads.setText(str(selected))

    def _invalidate(self, *_):
        self._preflight_stop.set()
        self._report = None
        if not self._busy:
            self._request = None
        self._start_button.setEnabled(False)

    def _install_texconv(self):
        if self._busy:
            return
        from Utils.wabbajack.textures import install_texture_tool
        self._status.setText(self.tr("Downloading verified texture tool…"))
        self._worker("texture", install_texture_tool)

    def _check(self):
        if self._busy or not self._package or not self._preflight_button.isEnabled():
            self._status.setText(self.tr("Open a modlist package first."))
            return
        from Utils.wabbajack.models import InstallRequest
        from Utils.wabbajack.games import source_roots
        from Utils.wabbajack.preflight import preflight
        game = self._games.get(self._target_game.currentText())
        if not game:
            self._status.setText(self.tr("Configure the required game before installing this modlist."))
            return
        profiles = [self._profiles.item(i).text() for i in range(self._profiles.count())
                    if self._profiles.item(i).checkState() == Qt.Checked]
        api = self._get_api()
        request = InstallRequest(self._package, copy.copy(game), Path(self._directory.text()).expanduser(),
            Path(self._downloads.text()).expanduser(), profiles, source_roots(self._package, game), api=api,
            fixes=[self._adjustments.item(i).data(Qt.UserRole) for i in range(self._adjustments.count())
                   if self._adjustments.item(i).checkState() == Qt.Checked],
            mode=self._mode.currentData(), gallery_id=self._entry.id if self._entry and not self._entry.id.startswith("local:") else "")
        from Utils.wabbajack.store import installation_info
        info = installation_info(request.directory)
        if info:
            request.game_roots.update({k: Path(v) for k, v in info.get("source_roots", {}).items()})
            request.gallery_id = request.gallery_id or info.get("gallery_id", "")
        if self._entry:
            request.gallery_metadata = {k: getattr(self._entry, k) for k in ("author", "image", "readme", "community", "download", "nsfw", "tags")}
        self._request = request
        self._preflight_stop = stop = threading.Event()
        self._preflight_button.setEnabled(False)
        self._status.setText(self.tr("Checking game files, downloads, disk space, and runtime requirements…"))
        def work():
            if api:
                try:
                    request.premium = bool(api.validate().is_premium)
                except Exception:
                    from Utils.ui.config import load_nexus_last_premium
                    request.premium = bool(load_nexus_last_premium())
            from Utils.ui.config import load_force_manual_install
            if load_force_manual_install():
                request.premium = False
            return request, preflight(request, stop)
        self._worker("preflight", work)

    def _start(self):
        if self._busy or not self._report or not self._report.ok:
            return
        if not self._can_install():
            self._status.setText(self.tr("Wait for the current install or deployment operation to finish."))
            return
        self._busy = True
        self.running_changed.emit(True)
        self._control = InstallControl()
        self._answers = queue.Queue()
        self._request.resolve_conflicts = self._wait_conflicts
        from gui_qt.collection_install_overlay import CollectionInstallOverlay
        from Utils.downloads.bandwidth import set_limit_mbps
        self._overlay = CollectionInstallOverlay.show_over(self, self._package.name,
            on_pause=self._pause, on_cancel=self._cancel, on_limit_change=set_limit_mbps)
        callbacks = InstallCallbacks(on_log=lambda text: safe_emit(self._progress, "log", (text,)),
                                    on_manual_mod=lambda payload: safe_emit(self._manual, payload))
        slots = {"on_status": "set_status", "on_display_total": "set_display_total",
            "on_agg_download": "set_agg", "on_dl_mod_start": "dl_start", "on_dl_mod_update": "dl_update",
            "on_dl_mod_finish": "dl_finish", "on_extract_add": "extract_add", "on_extract_remove": "extract_remove",
            "on_extract_queue": "extract_queue", "on_extract_update": "extract_update"}
        for attribute, slot in slots.items():
            setattr(callbacks, attribute, lambda *args, slot=slot: safe_emit(self._progress, slot, args))
        from Utils.wabbajack.install import run_install
        self._worker("install", lambda: run_install(self._request, callbacks=callbacks,
                      control=self._control, report=self._report))

    def _on_progress(self, method, args):
        if method == "log":
            self._log("[wabbajack] " + args[0])
            return
        if self._overlay:
            getattr(self._overlay, method)(*args)
        if method == "dl_finish" and self._manual_overlay and args[0] == self._manual_row:
            self._manual_overlay.dismiss()
            self._manual_overlay = None

    def _on_manual(self, payload):
        self._manual_row = payload["idx"]
        from gui_qt.collection_manual_overlay import CollectionManualOverlay
        if self._manual_overlay is None:
            self._manual_overlay = CollectionManualOverlay.show_over(self, self._package.name, "",
                len(self._package.archives), self._control.manual_queue, on_pause=self._pause, on_cancel=self._cancel)
        self._manual_overlay.update_mod(payload)
        self._manual_overlay._skip_btn.hide()

    def _pause(self):
        self._control.pause.set()
        self._control.stop.set()
        self._answers.put(None)

    def _cancel(self):
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
        if self._overlay:
            self._overlay.hide()
        self._conflict_table.setRowCount(len(conflicts))
        for row, conflict in enumerate(conflicts):
            item = QTableWidgetItem(conflict.path)
            item.setData(Qt.UserRole, conflict)
            self._conflict_table.setItem(row, 0, item)
            self._conflict_table.setItem(row, 1, QTableWidgetItem(conflict.reason))
            choice = QComboBox(self._conflict_table)
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
        self._answers.put(choices)
        self._stack.setCurrentIndex(1)
        if self._overlay:
            self._overlay.show()

    def _received(self, kind, result, error):
        if kind == "preflight":
            self._preflight_button.setEnabled(True)
            if self._request is None:
                return
        token, result = result
        if token != self._tokens.get(kind):
            return
        if error:
            self._status.setText(error)
            self._log("[wabbajack] " + error)
            if kind == "package" and self._package_url.startswith(("http://", "https://")):
                self._manual_package = True
                self._prepare_button.setText(self.tr("Download package in browser"))
                self._checks.setPlainText(self.tr("Download the .wabbajack file from {0}, then use Open .wabbajack to continue setup.").format(self._package_url))
                self._stack.setCurrentIndex(1)
        if kind == "gallery" and result:
            self._entries = result.entries
            for game in sorted({e.game for e in result.entries}):
                if self._game_filter.findData(game) < 0:
                    self._game_filter.addItem(game, game)
            for tag in sorted({tag for e in result.entries for tag in e.tags}):
                if self._tag.findData(tag) < 0:
                    self._tag.addItem(tag, tag)
            self._status.setText(self.tr("Using cached gallery information.") if result.cached else self.tr("Gallery loaded."))
            if result.warnings:
                self._status.setText(self._status.text() + self.tr(" {0} feeds unavailable.").format(len(result.warnings)))
                self._log("[wabbajack] " + "\n".join(result.warnings))
            self._render()
        elif kind == "readme" and result and self._package and result[0] == self._package.identity:
            self._description.setPlainText(result[1])
        elif kind == "package" and result:
            from Utils.wabbajack.games import matches_game
            self._package = result
            matched = next((name for name, game in self._games.items() if matches_game(game, result.game)), None)
            if matched:
                self._target_game.setCurrentText(matched)
            self._title.setText(f"{result.name} {result.version}")
            self._description.setPlainText(str(result.metadata.get("Description", "")) + "\n\n" + self.tr("Author: {0} · Downloads: {1:.1f} GiB · Installed: {2:.1f} GiB").format(
                result.metadata.get("Author", ""), sum(a.size for a in result.archives.values()) / 1024 ** 3,
                sum(d.size for d in result.directives) / 1024 ** 3))
            self._profiles.clear()
            selected = set(self._info.get("selected_profiles", [])) if self._info else set(result.profiles)
            if self._info and self._mode.currentData() == "update":
                selected.update(set(result.profiles) - set(self._info.get("authored_profiles", [])))
            for profile in result.profiles:
                item = QListWidgetItem(profile, self._profiles)
                item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
                item.setCheckState(Qt.Checked if profile in selected else Qt.Unchecked)
            self._profiles.setMaximumHeight(min(110, max(40, len(result.profiles) * 28 + 10)))
            self._adjustments.clear()
            from Utils.wabbajack.runtime import adjustments
            game = self._games.get(self._target_game.currentText())
            if game:
                accepted = self._info.get("fixes", []) if self._info else []
                for adjustment in adjustments(result, game):
                    item = QListWidgetItem(adjustment.label, self._adjustments)
                    item.setData(Qt.UserRole, adjustment.id)
                    item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
                    item.setCheckState(Qt.Checked if adjustment.id in accepted else Qt.Unchecked)
            self._setup_form.setRowVisible(self._adjustments, bool(self._adjustments.count()))
            self._stack.setCurrentIndex(1)
            if not self._info:
                self._game_selected()
                self._mode.setCurrentIndex(0)
            self._status.setText(self.tr("Package inspected. Check requirements before installing."))
        elif kind == "preflight" and result:
            request, report = result
            if request is not self._request:
                return
            self._report = report
            self._checks.setPlainText("\n".join(f"{c.status.upper()} — {c.name}: {c.detail}" for c in report.checks))
            self._start_button.setEnabled(report.ok)
            self._start_button.setText(self._mode.currentText())
            self._status.setText(self.tr("Requirements checked.") if report.ok else self.tr("Resolve the listed requirements before installing."))
        elif kind == "texture" and result:
            self._status.setText(self.tr("Texture tool installed. Run the requirements check again."))
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
            from Utils.wabbajack.store import installation_info
            self._info = installation_info(request.directory)
            if self._info:
                self._mode.setCurrentIndex(self._mode.findData("repair" if self._info.get("status") == "complete" else "resume"))
            if result:
                self._status.setText(result.message)
                if result.status == "complete":
                    self.installed.emit(request.game, result)

    def tab_closing(self):
        self._pause()
        for overlay in (self._overlay, self._manual_overlay):
            if overlay:
                overlay.dismiss()

    def tab_close_blocked(self):
        if self._busy:
            self._status.setText(self.tr("Pause or cancel the installation before closing this tab."))
        return self._busy

    def set_game(self, game):
        if not self._busy:
            self._game = game
            from Utils.wabbajack.games import configured_games
            self._games = configured_games()
            self._target_game.blockSignals(True)
            self._target_game.clear()
            self._target_game.addItems(list(self._games))
            if game:
                self._target_game.setCurrentText(game.name)
            self._target_game.blockSignals(False)
            self._refresh_installed()
            self._filter_changed()
