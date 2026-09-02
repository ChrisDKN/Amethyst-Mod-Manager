"""Change Version overlay - lists a mod's Nexus files so the user can install a
different version. Opens as a plugins-panel-scoped tab (covers the whole plugins
panel). Qt port of the Tk gui/mod_files_overlay.py; shares the pure highlight /
sort helpers in Utils.mods.versions.

The file list is fetched on a daemon thread (a Signal marshals the result back to
the UI thread - never a QThread). Installing a chosen file reuses the same
download → build_meta → install_fn flow as the Nexus browser tab.
"""

from __future__ import annotations

import threading

from PySide6.QtCore import Qt, Signal, QT_TRANSLATE_NOOP
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QLabel, QPushButton,
    QCheckBox, QTableWidget, QTableWidgetItem, QHeaderView, QAbstractItemView,
)

from gui_qt.theme_qt import (
    active_palette, bind_theme, _c, close_button, button_qss, qc,
)
from gui_qt.safe_emit import safe_emit
from Utils.mods.versions import resolve_latest_name_match, fmt_size, sort_key

# Translated at display time (setHorizontalHeaderLabels); register for lupdate.
_COLS = [
    QT_TRANSLATE_NOOP("ChangeVersionView", "File"),
    QT_TRANSLATE_NOOP("ChangeVersionView", "Version"),
    QT_TRANSLATE_NOOP("ChangeVersionView", "Category"),
    QT_TRANSLATE_NOOP("ChangeVersionView", "Size"),
    "",
]

_ROW_THEME_ROLE = Qt.UserRole + 41


class _LegendBar(QWidget):
    """Highlight key for the version list. Centered, and reflows between one row
    (4 across, when there's width) and 2×2 (when narrow) so text never clips. In
    2×2 the columns line up: installed/older on the left, newest/none on the
    right (col 0 = items 0 & 2, col 1 = items 1 & 3)."""

    _COL_GAP = 16
    _MARGIN = 12

    def __init__(self, p):
        super().__init__()
        self._grid = QGridLayout(self)
        self._grid.setContentsMargins(self._MARGIN, 4, self._MARGIN, 4)
        self._grid.setHorizontalSpacing(self._COL_GAP)
        self._grid.setVerticalSpacing(2)
        legend_items = [
            ("TEXT_OK_BRIGHT", "Currently installed"),
            ("STATUS_QUEUED", "Newest matching version"),
            ("TEXT_ERR_BRIGHT", "Older matching version"),
            (None, "No name match"),
        ]
        self._entries = [self._make_entry(p, key, lbl)
                         for key, lbl in legend_items]
        self._cols = 0          # force first layout
        self._one_row_min = self._one_row_width()
        self._relayout(force=True)

    def _make_entry(self, p, color_key, label) -> QWidget:
        e = QWidget()
        h = QHBoxLayout(e); h.setContentsMargins(0, 0, 0, 0); h.setSpacing(6)
        sw = QLabel(); sw.setFixedSize(12, 12)
        bg = _c(p, color_key) if color_key else _c(p, "BG_DEEP")
        border = ("" if color_key else
                  f" border:1px solid {_c(p,'TEXT_DIM')};")
        sw.setStyleSheet(f"background:{bg}; border-radius:2px;{border}")
        h.addWidget(sw)
        lbl = QLabel(label); lbl.setStyleSheet(f"color:{_c(p,'TEXT_DIM')};")
        h.addWidget(lbl)
        return e

    def _one_row_width(self) -> int:
        """Pixel width needed to show all 4 entries on one row."""
        w = sum(e.sizeHint().width() for e in self._entries)
        w += self._COL_GAP * (len(self._entries) - 1)
        return w + self._MARGIN * 2

    def _relayout(self, force=False):
        avail = self.width()
        cols = 4 if avail >= self._one_row_min else 2
        if cols == self._cols and not force:
            return
        self._cols = cols
        # Detach all entries (without deleting them).
        while self._grid.count():
            self._grid.takeAt(0)
        for e in self._entries:
            self._grid.removeWidget(e)
        # Content occupies grid columns 1..cols; cols 0 and cols+1 stretch so the
        # whole block stays centered no matter how wide the panel gets. In 2×2,
        # placing row-major keeps the top pair (installed, newest) above the
        # bottom pair (older, none): col 0 = installed/older, col 1 = newest/none.
        for i, e in enumerate(self._entries):
            r = (i // cols) if cols == 2 else 0
            c = (i % cols) if cols == 2 else i
            self._grid.addWidget(e, r, c + 1, Qt.AlignLeft | Qt.AlignVCenter)
            e.show()
        for c in range(self._grid.columnCount()):
            self._grid.setColumnStretch(c, 0)
        self._grid.setColumnStretch(0, 1)
        self._grid.setColumnStretch(cols + 1, 1)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._relayout()


class ChangeVersionView(QWidget):
    """Scoped-tab body for picking a mod version to install."""

    # (files | None, error_msg, fetch_gen) from the fetch worker → UI thread.
    # fetch_gen guards against a stale fetch landing after a retarget.
    _files_ready = Signal(object, object, int)
    # (archive | None, meta | None) from the download worker → UI thread.
    _download_done = Signal(object, object)
    # (file, is_premium) from the premium-check worker → UI thread.
    _premium_checked = Signal(object, object)
    # Browser-download watch progress (bytes; 64-bit: >2GB files).
    _watch_progress = Signal("qlonglong", "qlonglong")
    # Premium API-download progress → shared item (key, name, done, total).
    _api_progress = Signal(str, str, "qlonglong", "qlonglong")

    def __init__(self, api, game, mod_name, meta, install_fn,
                 on_close, log_fn=None, progress_fn=None):
        super().__init__()
        self._api = api
        self._game = game
        self._mod_name = mod_name
        self._meta = meta
        self._install_fn = install_fn or (lambda paths, metas=None: None)
        self._on_close = on_close or (lambda: None)
        self._log = log_fn or (lambda _m: None)
        # progress_fn(key, name, downloaded, total) drives the shared download
        # item in the notification menu; total<0 marks
        # this key finished. Defaults to a no-op if the host didn't supply one.
        self._progress_fn = progress_fn or (lambda *args: None)
        self._dl_key = None    # progress-item key while a manual watch is armed
        self._installing = False
        self._files = None          # cached (sorted) file list for _meta.mod_id
        self._fetch_gen = 0         # invalidates in-flight fetches on retarget
        self._install_prev = None   # mod name captured when an install started
        self._install_status = False  # status line shows "Installing…"
        # (file_id, watcher, install_btn) while a non-premium install waits
        # for a browser download; the row's Install button shows Cancel.
        self._manual_watch = None
        self._pending_btn = None    # button of the install being prepped
        self._install_btns: list = []   # per-row buttons, for live relabelling
        # The destroyed hook must not touch self (C++ side is gone by then) -
        # it captures these holders directly. _key_holder["k"] tracks the live
        # progress key so a mid-watch tab close still clears the item.
        self._watch_holder: dict = {}
        self._key_holder: dict = {}
        self._cancel_holder: dict = {}

        def _stop_watch(*_, h=self._watch_holder, kh=self._key_holder,
                        ch=self._cancel_holder, pf=self._progress_fn):
            for w in list(h.values()):
                w.stop()
            h.clear()
            for event in list(ch.values()):
                event.set()
            ch.clear()
            k = kh.pop("k", None)
            if k is not None:
                pf(k, "", 0, -1)
        self.destroyed.connect(_stop_watch)

        self.setObjectName("ChangeVersionView")
        self._files_ready.connect(self._on_files_ready)
        self._download_done.connect(self._on_download_done)
        self._premium_checked.connect(self._on_premium_checked)
        self._watch_progress.connect(self._on_watch_progress)
        self._api_progress.connect(
            lambda k, n, d, t: self._progress_fn(k, n, int(d), int(t)))

        self._build()
        bind_theme(self, roles={
            "BG_GREEN_DEEP", "TEXT_OK_BRIGHT", "BG_ORANGE_DEEP",
            "STATUS_QUEUED", "BG_RED_DEEP", "TEXT_ERR_BRIGHT",
        })
        self._start_fetch()

    def refresh_theme(self, p):
        """Recolour populated rows without replacing their widgets or state."""
        for row in range(self._table.rowCount()):
            for col in range(4):
                item = self._table.item(row, col)
                if item is None:
                    continue
                spec = item.data(_ROW_THEME_ROLE)
                if not spec:
                    continue
                bg_key, fg_key = spec
                if bg_key:
                    item.setBackground(qc(p, bg_key))
                if fg_key:
                    item.setForeground(qc(p, fg_key))
        self._table.viewport().update()

    # ---- layout -----------------------------------------------------------
    def _build(self):
        p = active_palette()
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)

        # Toolbar: title + Ignore Update + Close.
        bar = QWidget(); bar.setObjectName("HeaderBar")
        hb = QHBoxLayout(bar); hb.setContentsMargins(12, 8, 8, 8); hb.setSpacing(8)
        self._title = QLabel(self.tr("Change Version - {0}").format(self._mod_name))
        self._title.setStyleSheet(f"color:{_c(p,'TEXT_MAIN')}; font-weight:600;")
        hb.addWidget(self._title)
        hb.addStretch(1)

        self._ignore_cb = QCheckBox(self.tr("Ignore Update"))
        self._ignore_cb.setToolTip(
            self.tr("Stop flagging this mod as having an update until a newer version "
            "than the current latest appears."))
        self._ignore_cb.setChecked(bool(getattr(self._meta, "ignore_update", False)))
        self._ignore_cb.toggled.connect(self._on_ignore_toggled)
        hb.addWidget(self._ignore_cb)

        close = close_button(pal=p)
        close.clicked.connect(lambda: self._on_close())
        hb.addWidget(close)
        v.addWidget(bar)

        # Highlight key - explains the row tints. Reflows 1 row ↔ 2×2, centered.
        v.addWidget(_LegendBar(p))

        # File table.
        self._table = QTableWidget(0, len(_COLS))
        self._table.setHorizontalHeaderLabels(
            [self.tr(c) if c else "" for c in _COLS])
        self._table.verticalHeader().setVisible(False)
        self._table.setSelectionMode(QAbstractItemView.NoSelection)
        self._table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self._table.setFocusPolicy(Qt.NoFocus)
        self._table.setShowGrid(False)
        hh = self._table.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.Stretch)        # File
        for c in (1, 2, 3):
            hh.setSectionResizeMode(c, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(4, QHeaderView.ResizeToContents)  # buttons
        v.addWidget(self._table, 1)

        # Status line (loading / empty / error).
        self._status = QLabel(self.tr("Loading files…"))
        self._status.setStyleSheet(f"color:{_c(p,'TEXT_DIM')}; padding:8px 12px;")
        v.addWidget(self._status)

    # ---- fetch ------------------------------------------------------------
    def _start_fetch(self):
        domain = self._effective_domain()
        mod_id = int(getattr(self._meta, "mod_id", 0) or 0)
        self._fetch_gen += 1
        gen = self._fetch_gen

        def worker():
            try:
                resp = self._api.get_mod_files(domain, mod_id)
                safe_emit(self._files_ready, list(resp.files), None, gen)
            except Exception as exc:
                safe_emit(self._files_ready, None, str(exc), gen)

        threading.Thread(target=worker, daemon=True, name="change-version-fetch").start()

    def _on_files_ready(self, files, error, gen):
        if gen != self._fetch_gen:
            return    # retargeted to another mod while this fetch ran
        if error is not None:
            self._status.setText(self.tr("Could not load files: {0}").format(error))
            self._status.setVisible(True)
            return
        if not files:
            self._status.setText(self.tr("No files found."))
            self._status.setVisible(True)
            return
        self._status.setVisible(False)
        self._files = sorted(files, key=sort_key)
        self._populate(self._files)

    # ---- table population + highlight ------------------------------------
    @staticmethod
    def _download_only() -> bool:
        from Utils.ui.config import load_download_only
        try:
            return bool(load_download_only())
        except Exception:
            return False

    def _install_label(self) -> str:
        """Install, or Download while 'Download only' is on."""
        return self.tr("Download") if self._download_only() else self.tr("Install")

    def refresh_install_labels(self):
        """Re-read 'Download only' and relabel the row buttons in place."""
        # NOT a _populate(): that deletes the widget a pending manual watch still
        # references. The watching row keeps its Cancel (_end_manual_watch restores it).
        text = self._install_label()
        watch_btn = self._manual_watch[2] if self._manual_watch else None
        live = []
        for b in list(self._install_btns):
            try:
                if b is not watch_btn:
                    b.setText(text)
                live.append(b)
            except RuntimeError:
                pass        # row already rebuilt; the C++ side is gone
        self._install_btns = live

    def _populate(self, files):
        installed_id = int(getattr(self._meta, "file_id", 0) or 0)
        match_id, old_ids = resolve_latest_name_match(
            files, installed_id, self._mod_name)
        domain = self._effective_domain()
        mod_id = int(getattr(self._meta, "mod_id", 0) or 0)

        p = active_palette()
        self._install_btns = []      # the old row widgets are about to be replaced
        if not self._install_status:
            self._status.setVisible(False)   # drop a stale download-only notice
        _btn_label = self._install_label()
        self._table.setRowCount(len(files))
        for row, f in enumerate(files):
            is_installed = installed_id > 0 and f.file_id == installed_id
            is_match = not is_installed and match_id > 0 and f.file_id == match_id
            is_old = not is_installed and not is_match and f.file_id in old_ids
            if is_installed:
                bg_key, name_fg_key = "BG_GREEN_DEEP", "TEXT_OK_BRIGHT"
            elif is_match:
                bg_key, name_fg_key = "BG_ORANGE_DEEP", "STATUS_QUEUED"
            elif is_old:
                bg_key, name_fg_key = "BG_RED_DEEP", "TEXT_ERR_BRIGHT"
            else:
                bg_key = name_fg_key = None

            name_text = (f.name or f.file_name or "") + ("  ✓" if is_installed else "")
            size = f.size_in_bytes or (f.size_kb * 1024 if f.size_kb else 0)
            cells = [name_text, f.version or "",
                     (f.category_name or "").capitalize(), fmt_size(size)]
            for col, text in enumerate(cells):
                it = QTableWidgetItem(text)
                fg_key = name_fg_key if col == 0 else None
                it.setData(_ROW_THEME_ROLE, (bg_key, fg_key))
                if bg_key is not None:
                    it.setBackground(qc(p, bg_key))
                if fg_key is not None:
                    it.setForeground(qc(p, fg_key))
                self._table.setItem(row, col, it)

            # Buttons cell (View + Install).
            cell = QWidget()
            if bg_key is not None:
                cell.setAutoFillBackground(True)
                cell.setStyleSheet(f"background:{_c(p, bg_key)};")
            cb = QHBoxLayout(cell); cb.setContentsMargins(8, 4, 8, 4); cb.setSpacing(6)
            view_url = (f"https://www.nexusmods.com/{domain}/mods/{mod_id}"
                        f"?tab=files&file_id={f.file_id}")
            view_btn = QPushButton(self.tr("View")); view_btn.setCursor(Qt.PointingHandCursor)
            view_btn.setStyleSheet(button_qss("BTN_GREY", padding="4px 10px"))
            view_btn.clicked.connect(lambda _=False, u=view_url: self._open_url(u))
            cb.addWidget(view_btn)
            inst_btn = QPushButton(_btn_label); inst_btn.setCursor(Qt.PointingHandCursor)
            # Explicit success colour so the row tint (set on the parent cell)
            # can't bleed into the button background.
            inst_btn.setStyleSheet(button_qss("BTN_SUCCESS", padding="4px 10px"))
            inst_btn.clicked.connect(
                lambda _=False, ff=f, b=inst_btn: self._install_file(ff, b))
            cb.addWidget(inst_btn)
            self._install_btns.append(inst_btn)
            cb.addStretch(1)
            self._table.setCellWidget(row, 4, cell)

    # ---- retargeting (tab stays open) -------------------------------------
    def current_mod_name(self) -> str:
        """The mod this tab is currently showing."""
        return self._mod_name

    def busy(self) -> bool:
        """True while an install kicked off here is still in flight (premium
        check, download, browser-download watch, or the async install itself) -
        retargeting then would yank the flow out from under the user."""
        return bool(self._installing or self._manual_watch is not None
                    or self._install_status)

    def clear_install_status(self):
        """Hide the 'Installing…' notice (the install finished or failed)."""
        if self._install_status:
            self._install_status = False
            self._status.setVisible(False)

    def retarget(self, mod_name: str, meta):
        """Point the open tab at *mod_name*: title, Ignore-Update state and row
        highlights follow. The file list is only re-fetched when the Nexus mod
        id actually changed; a same-mod retarget (install finished, or the
        previous version was removed and the folder name swapped) just repaints
        the highlights from the cached list."""
        old_mod_id = int(getattr(self._meta, "mod_id", 0) or 0)
        # The repopulate below deletes the row buttons a pending browser-watch
        # flow still references - stop it first (same as an Install-click toggle).
        if self._manual_watch is not None:
            self.cancel_manual_watch()
        self._pending_btn = None
        self._mod_name = mod_name
        self._meta = meta
        self._title.setText(self.tr("Change Version - {0}").format(mod_name))
        self._ignore_cb.blockSignals(True)
        self._ignore_cb.setChecked(bool(getattr(meta, "ignore_update", False)))
        self._ignore_cb.blockSignals(False)
        self.clear_install_status()
        if int(getattr(meta, "mod_id", 0) or 0) != old_mod_id or not self._files:
            self._files = None
            self._table.setRowCount(0)
            self._status.setText(self.tr("Loading files…"))
            self._status.setVisible(True)
            self._start_fetch()
        else:
            self._populate(self._files)

    # ---- actions ----------------------------------------------------------
    def _open_url(self, url):
        try:
            from Utils.environment.xdg import open_url
            open_url(url)
        except Exception:
            pass

    def _on_ignore_toggled(self, state):
        """Write ignore_update (+ ignored_version) to the mod's meta.ini. The
        modlist flag refresh happens when the overlay closes (_reload_modlist)."""
        staging = getattr(self._game, "get_effective_mod_staging_path", None)
        try:
            from Nexus.nexus_meta import read_meta, write_meta
            mp = (self._game.get_effective_mod_staging_path()
                  if staging else None)
            if mp is None:
                return
            meta_path = mp / self._mod_name / "meta.ini"
            m = read_meta(meta_path)
            m.ignore_update = bool(state)
            if state:
                m.has_update = False
                m.ignored_version = m.latest_version
            else:
                m.ignored_version = ""
            write_meta(meta_path, m)
            self._meta = m
        except Exception as exc:
            self._log(f"Nexus: could not save ignore flag - {exc}")

    def _domain_and_mod_id(self):
        domain = self._effective_domain()
        return domain, int(getattr(self._meta, "mod_id", 0) or 0)

    def _effective_domain(self) -> str:
        from Nexus.nexus_meta import normalise_game_domain
        return (normalise_game_domain(
                    getattr(self._meta, "game_domain", "") or "")
                or normalise_game_domain(
                    getattr(self._game, "nexus_game_domain", "") or ""))

    def _info_stub(self, domain, mod_id):
        """A minimal mod_info-like fallback (used only if the API lookup
        fails). Real installs pass a full NexusModInfo so author / uploader /
        summary / category land in meta.ini - fetch the same here."""
        class _Info:
            pass
        stub = _Info()
        stub.mod_id = mod_id
        stub.domain_name = domain
        stub.name = getattr(self._meta, "nexus_name", "") or self._mod_name
        return stub

    def _install_file(self, f, btn=None):
        if self._manual_watch is not None:
            # Waiting for a browser download - any Install click stops that
            # watch; the watched row's own button (showing Cancel) just stops.
            watched_fid = self._manual_watch[0]
            self.cancel_manual_watch()
            if watched_fid == f.file_id:
                return
        if self._installing:
            return
        self._installing = True
        self._pending_btn = btn
        # Captured NOW: a retarget (selection follow) mid-download must not
        # change which mod the install replaces.
        self._install_prev = self._mod_name

        # Premium gate ([dev] force_manual_install honoured - same switch as
        # the browser/collections): free accounts can't use the download API,
        # so they get the browser-download watch flow instead.
        def check():
            premium = False
            try:
                premium = bool(self._api.validate().is_premium)
                if premium:
                    from Utils.ui.config import load_force_manual_install
                    if load_force_manual_install():
                        self._log("Nexus: [dev] force_manual_install - using "
                                  "the manual browser-download flow.")
                        premium = False
            except Exception as exc:
                self._log(f"Nexus: premium check failed ({exc}) - using the "
                          "manual browser-download flow.")
            safe_emit(self._premium_checked, f, premium)

        threading.Thread(target=check, daemon=True,
                         name="change-version-premium").start()

    def _on_premium_checked(self, f, is_premium):
        btn, self._pending_btn = self._pending_btn, None
        if is_premium:
            self._start_api_download(f)
        else:
            self._start_manual_flow(f, btn)

    def _start_api_download(self, f):
        domain, mod_id = self._domain_and_mod_id()
        dl_label = f.file_name or f.name or self._mod_name
        self._log(f"Nexus: downloading {dl_label}…")
        stub = self._info_stub(domain, mod_id)
        # Shared notification-menu progress item (parity with the browser tab);
        # cleared in _on_download_done. Indeterminate until first progress_cb.
        self._dl_key = f"chv-api-{mod_id}-{f.file_id}"
        self._key_holder["k"] = self._dl_key
        dl_key = self._dl_key
        cancel = threading.Event()
        self._cancel_holder["event"] = cancel
        self._progress_fn(dl_key, dl_label, 0, 0, cancel.set)

        def worker():
            archive = meta = None
            try:
                from Nexus.nexus_download import NexusDownloader
                from Utils.config_paths import get_download_cache_dir_for_game
                from Nexus.nexus_meta import build_meta_from_download
                dest = get_download_cache_dir_for_game(
                    getattr(self._game, "name", "") or "")
                size = (f.size_in_bytes or 0) or (f.size_kb * 1024)
                result = NexusDownloader(self._api, download_dir=dest).download_file(
                    game_domain=domain, mod_id=mod_id, file_id=f.file_id,
                    dest_dir=dest, known_file_name=f.file_name,
                    expected_size_bytes=size,
                    progress_cb=lambda d, t: safe_emit(
                        self._api_progress, dl_key, dl_label, int(d), int(t)),
                    cancel=cancel)
                if result.success and result.file_path is not None:
                    archive = str(result.file_path)
                    # Fetch full mod info (author, uploader, summary, category)
                    # so the change-version install stamps the same metadata as
                    # a fresh install or reinstall. Use GraphQL (like the NXM /
                    # browser path) so this costs NO REST rate-limit call - the
                    # download itself already spends the one allowed call. Fall
                    # back to the stub only if the lookup fails.
                    info = stub
                    try:
                        fetched, _ = self._api.get_mod_and_file_info_graphql(
                            domain, mod_id, f.file_id)
                        if fetched is not None:
                            info = fetched
                    except Exception:
                        pass
                    try:
                        meta = build_meta_from_download(
                            game_domain=domain, mod_id=mod_id, file_id=f.file_id,
                            archive_name=result.file_name, mod_info=info, file_info=f)
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
            safe_emit(self._download_done, archive, meta)

        threading.Thread(target=worker, daemon=True, name="change-version-dl").start()

    # ---- non-premium: file's download page + folder watch ------------------
    def _start_manual_flow(self, f, btn):
        """Non-premium install via the shared start_manual_install flow (skip
        the browser when the archive is already downloaded, else open the
        file's own download page + watch the download folders). *btn* becomes
        a red Cancel while waiting."""
        from Nexus.manual_download_watch import start_manual_install
        self._installing = False
        domain, mod_id = self._domain_and_mod_id()
        fname = f.file_name or f.name or ""
        # Drive the shared notification progress item (parity with the browser
        # tab) - indeterminate until the watcher can see the in-flight download.
        self._dl_key = f"chv-man-{mod_id}-{f.file_id}"
        self._key_holder["k"] = self._dl_key
        self._progress_fn(self._dl_key, fname, 0, 0)

        # Callbacks run on the WATCHER thread - marshal via Signals.
        def on_archive(path, meta, _file):
            safe_emit(self._download_done, str(path), meta)

        def on_progress(done, total):
            safe_emit(self._watch_progress, int(done), int(total))

        def on_timeout():
            self._log(f"Nexus: stopped waiting for a browser download of "
                      f"'{fname}' (nothing arrived).")
            safe_emit(self._download_done, None, None)

        watcher, already_downloaded = start_manual_install(
            api=self._api, game_domain=domain, mod_id=mod_id, files=[f],
            open_url_fn=self._open_url, log_fn=self._log, log_label=fname,
            mod_info_fallback=self._info_stub(domain, mod_id),
            on_archive=on_archive, on_progress=on_progress,
            on_timeout=on_timeout)
        if not already_downloaded:
            self._status.setText(self.tr(
                "Waiting for the browser download of '{0}' - click Cancel to "
                "stop.").format(fname))
            self._status.setVisible(True)
            if btn is not None:
                btn.setText(self.tr("Cancel"))
                btn.setStyleSheet(button_qss("BTN_DANGER", padding="4px 10px"))
        self._manual_watch = (f.file_id, watcher, btn)
        self._watch_holder["w"] = watcher

    def _on_watch_progress(self, done, total):
        from Utils.downloads.cache import format_size
        self._status.setText(self.tr(
            "Waiting for the browser download - {0} / {1}").format(
            format_size(done), format_size(total)))
        # Feed real bytes into the shared progress item (was indeterminate).
        if self._dl_key is not None:
            self._progress_fn(self._dl_key, self._mod_name, int(done), int(total))

    def _end_manual_watch(self):
        """Stop the watcher (if any) and restore the row button + status."""
        # Clear this download's progress item (total<0 = finished/cancelled).
        if self._dl_key is not None:
            self._progress_fn(self._dl_key, "", 0, -1)
            self._dl_key = None
            self._key_holder.pop("k", None)
        t, self._manual_watch = self._manual_watch, None
        self._watch_holder.clear()
        if t is None:
            return
        _fid, watcher, btn = t
        watcher.stop()
        if btn is not None:
            btn.setText(self._install_label())
            btn.setStyleSheet(button_qss("BTN_SUCCESS", padding="4px 10px"))
        self._status.setVisible(False)

    def cancel_manual_watch(self, mod_id=None, game_domain=""):
        """Stop the pending browser-download watch: the Install-click toggle,
        or the app when an nxm:// download for this mod arrives ('Download
        with Mod Manager' - the nxm flow installs it, so the watch must not)."""
        if self._manual_watch is None:
            return
        if game_domain and str(game_domain).strip().lower() != self._effective_domain():
            return
        if mod_id is not None and \
                int(mod_id) != int(getattr(self._meta, "mod_id", 0) or 0):
            return
        self._end_manual_watch()
        self._log("Nexus: cancelled download detection.")

    def _on_download_done(self, archive, meta):
        self._installing = False
        self._cancel_holder.clear()
        # Clears the progress item + (for the manual path) restores the row button.
        self._end_manual_watch()
        prev, self._install_prev = self._install_prev, None
        if not archive:
            return
        self._log(f"Nexus: downloaded → {archive}")
        # Pass the mod we're updating so the app can offer "Remove previous
        # version?" if the new file installs under a different folder name.
        metas = {archive: meta} if meta is not None else None
        try:
            queued = self._install_fn([archive], metas,
                                      previous_mod_name=prev or self._mod_name)
        except TypeError:
            # install_fn without the previous_mod_name kwarg (defensive).
            queued = self._install_fn([archive], metas)
        # The host returns False when 'Download only' diverted it. Trust that
        # rather than re-reading the setting - a toggle between the two reads
        # would leave _install_status set with no install to ever clear it, and
        # busy() would block retargeting this tab for good.
        if queued is False:
            self._status.setText(self.tr(
                "Downloaded - install it from the Downloads tab."))
            self._status.setVisible(True)
            return
        # The tab stays open - the install runs asynchronously and the host
        # retargets this view (refreshing the highlights) once it lands.
        self._install_status = True
        self._status.setText(self.tr(
            "Installing - the list will refresh when it finishes."))
        self._status.setVisible(True)
