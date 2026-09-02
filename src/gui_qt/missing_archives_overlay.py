"""Pre-export gate: offer to redownload missing original archives.

File-edit binary patches and (for collection exports) FOMOD choice recovery
both need the mod's pristine download archive. When the export preflight
(``Utils.collections.export.missing_archive_report``) finds mods whose archive is
gone from the download cache, this overlay lists them and offers to fetch
the exact installed file again: a direct API download for premium accounts,
or the browser "Slow download" page plus the download-folder watcher
(``manual_download_watch``) for free accounts. Fetched archives are placed
in the per-game download cache with their ``.fileid`` sidecar, so the
export's normal lookups find them.

``on_done(True)`` = go ahead with the export (any still-missing archives
fall back to the existing per-mod warnings); ``on_done(False)`` = cancel.

The fetch worker is a plain thread; every UI update crosses back via
``safe_emit`` and the overlay's own Signals. Cancel (button, Esc, backdrop)
funnels through ``_finish``, which stops the worker and any active watcher.
"""

from __future__ import annotations

import threading
from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QHBoxLayout, QLabel, QListWidget, QPushButton

from gui_qt.overlay_base import OverlayBase
from gui_qt.safe_emit import safe_emit
from gui_qt.theme_qt import active_palette, _c, contrast_text


def _fmt_mb(n: int) -> str:
    return f"{(n or 0) / 1024 ** 2:.1f} MB"


class MissingArchivesOverlay(OverlayBase):
    CARD_W = 600
    CARD_H = 460
    ESC_RESULT = False

    _status = Signal(str)              # progress line under the list
    _item_state = Signal(int, str)     # report index → state suffix
    _fetch_done = Signal(int, int)     # (fetched, failed)
    _manual_page = Signal(str)         # current manual download-page url ("" = hide)

    def __init__(self, host, report: list, *, api, game_name: str,
                 log_fn=None, on_done=None):
        super().__init__(host, on_done=on_done)
        self._report = list(report)
        self._api = api
        self._game_name = game_name or ""
        self._log = log_fn or (lambda _m: None)
        self._cancel_evt = threading.Event()
        self._skip_evt = threading.Event()
        self._watcher = None
        self._manual_url = ""
        self._fetching = False

        p = active_palette()
        _card, v = self._make_card(
            "MissingArchivesCard",
            extra_qss=(
                f" #DangerButton {{ background:{_c(p, 'BTN_DANGER')};"
                f" color:{contrast_text(_c(p, 'BTN_DANGER'))};"
                f" border:none; border-radius:4px; padding:6px 14px;"
                f" font-weight:600; }}"
                f" #DangerButton:hover {{ background:{_c(p, 'BTN_DANGER_HOV')}; }}"))

        title = QLabel(self.tr("Original archives needed"))
        title.setStyleSheet(
            f"color:{_c(p, 'TEXT_MAIN')}; font-weight:600; font-size:16px;")
        v.addWidget(title)

        body = QLabel(self.tr(
            "{0} mod(s) use export features that read their original download "
            "archive (file edits become patches against it; installer choices "
            "are mapped through its FOMOD config), but the archive is no "
            "longer in the download cache.\n\nDownload fetches the exact "
            "installed file again - automatically with a premium account, "
            "via each file's download page otherwise. Continuing without "
            "leaves those edits or choices out of the export."
        ).format(len(self._report)))
        body.setStyleSheet(f"color:{_c(p, 'TEXT_DIM')}; font-size:13px;")
        body.setWordWrap(True)
        v.addWidget(body)

        self._list = QListWidget()
        self._list.setObjectName("ConfirmList")
        self._list.setSelectionMode(QListWidget.NoSelection)
        self._list.setFocusPolicy(Qt.NoFocus)
        self._list.setStyleSheet(
            f"#ConfirmList {{ background:{_c(p, 'BG_LIST')};"
            f" color:{_c(p, 'TEXT_DIM')}; border:1px solid {_c(p, 'BORDER')};"
            f" border-radius:4px; font-size:12px; padding:2px; }}"
            f" #ConfirmList::item {{ padding:2px 4px; }}")
        self._item_base: list = []
        need_labels = {
            "edits": self.tr("file edits"),
            "choices": self.tr("installer choices"),
        }
        for e in self._report:
            needs = ", ".join(need_labels.get(n, n) for n in e.get("needs", []))
            text = f"{e['name']} - {needs}"
            if not e.get("downloadable"):
                text += self.tr(" (can't redownload: no Nexus file id)")
            self._item_base.append(text)
            self._list.addItem(text)
        v.addWidget(self._list, 1)

        self._status_lbl = QLabel("")
        self._status_lbl.setStyleSheet(
            f"color:{_c(p, 'TEXT_DIM')}; font-size:12px;")
        self._status_lbl.setWordWrap(True)
        self._status_lbl.hide()
        v.addWidget(self._status_lbl)

        # Manual-flow helpers: shown only while waiting on a browser download.
        manual_row = QHBoxLayout()
        manual_row.setSpacing(8)
        self._open_btn = QPushButton(self.tr("Open Download Page"))
        self._open_btn.setObjectName("FormButton")
        self._open_btn.setCursor(Qt.PointingHandCursor)
        self._open_btn.clicked.connect(self._open_manual_page)
        self._open_btn.hide()
        manual_row.addWidget(self._open_btn)
        self._skip_btn = QPushButton(self.tr("Skip this mod"))
        self._skip_btn.setObjectName("FormButton")
        self._skip_btn.setCursor(Qt.PointingHandCursor)
        self._skip_btn.clicked.connect(self._skip_evt.set)
        self._skip_btn.hide()
        manual_row.addWidget(self._skip_btn)
        manual_row.addStretch(1)
        v.addLayout(manual_row)

        bar = QHBoxLayout()
        bar.setSpacing(8)
        bar.addStretch(1)
        self._cancel_btn = QPushButton(self.tr("Cancel"))
        self._cancel_btn.setObjectName("FormButton")
        self._cancel_btn.setCursor(Qt.PointingHandCursor)
        self._cancel_btn.clicked.connect(lambda: self._finish(False))
        bar.addWidget(self._cancel_btn)
        self._continue_btn = QPushButton(self.tr("Continue without"))
        self._continue_btn.setObjectName("FormButton")
        self._continue_btn.setCursor(Qt.PointingHandCursor)
        self._continue_btn.clicked.connect(lambda: self._finish(True))
        bar.addWidget(self._continue_btn)
        self._dl_btn = QPushButton(self.tr("Download"))
        self._dl_btn.setObjectName("PrimaryButton")
        self._dl_btn.setCursor(Qt.PointingHandCursor)
        self._dl_btn.clicked.connect(self._start_fetch)
        self._dl_btn.setEnabled(any(e.get("downloadable")
                                    for e in self._report))
        bar.addWidget(self._dl_btn)
        v.addLayout(bar)

        self._status.connect(self._on_status)
        self._item_state.connect(self._on_item_state)
        self._fetch_done.connect(self._on_fetch_done)
        self._manual_page.connect(self._on_manual_page)

        # Worker-side status templates: .tr() belongs on the UI thread.
        self._tpl_downloading = self.tr("({0}/{1}) Downloading '{2}'… {3} / {4}")
        self._tpl_starting = self.tr("({0}/{1}) Fetching '{2}'…")
        self._tpl_waiting = self.tr(
            "({0}/{1}) Waiting for the browser download of '{2}'…")
        self._tpl_waiting_prog = self.tr(
            "({0}/{1}) Waiting for the browser download of '{2}'… {3} / {4}")

        self._present()

    @classmethod
    def show_over(cls, host, report, **kw):
        top = host.window() if host is not None else None
        return cls(top or host, report, **kw)

    # -- lifecycle -----------------------------------------------------------
    def _finish(self, result=None):
        # Stop the fetch machinery on ANY exit (button, Esc, host teardown):
        # the worker checks the event between mods and passes it to the
        # downloader; an armed watcher is stopped so its thread dies too.
        self._cancel_evt.set()
        w = self._watcher
        if w is not None:
            try:
                w.stop()
            except Exception:
                pass
        super()._finish(result)

    # -- fetch ---------------------------------------------------------------
    def _start_fetch(self):
        if self._fetching:
            return
        self._fetching = True
        self._dl_btn.setEnabled(False)
        self._continue_btn.setEnabled(False)
        self._status_lbl.show()
        self._status_lbl.setText(self.tr("Checking account…"))
        threading.Thread(target=self._fetch_worker, daemon=True,
                         name="export-archive-fetch").start()

    def _fetch_worker(self):
        """Worker thread: fetch every downloadable entry sequentially."""
        premium = False
        try:
            premium = bool(self._api is not None
                           and self._api.validate().is_premium)
        except Exception:
            premium = False
        try:
            from Utils.ui.config import load_force_manual_install
            if premium and load_force_manual_install():
                premium = False
        except Exception:
            pass

        fetched = failed = 0
        total = len(self._report)
        for idx, entry in enumerate(self._report):
            if self._cancel_evt.is_set():
                return
            if not entry.get("downloadable"):
                failed += 1
                continue
            safe_emit(self._status, self._tpl_starting.format(
                idx + 1, total, entry["name"]))
            try:
                ok = (self._fetch_premium(entry, idx, total) if premium
                      else self._fetch_manual(entry, idx, total))
            except Exception as exc:
                self._log(f"[export] archive redownload failed for "
                          f"'{entry['name']}': {exc}")
                ok = False
            if self._cancel_evt.is_set():
                return
            if ok:
                fetched += 1
                safe_emit(self._item_state, idx, "ok")
            else:
                failed += 1
                safe_emit(self._item_state, idx, "failed")
        safe_emit(self._fetch_done, fetched, failed)

    def _fetch_premium(self, entry: dict, idx: int, total: int) -> bool:
        import time
        from Nexus.nexus_download import NexusDownloader, _write_sidecar_file_id
        from Utils.config_paths import get_download_cache_dir_for_game

        last = [0.0]

        def _cb(done, tot):
            now = time.monotonic()
            if now - last[0] < 0.25:
                return
            last[0] = now
            safe_emit(self._status, self._tpl_downloading.format(
                idx + 1, total, entry["name"], _fmt_mb(done), _fmt_mb(tot)))

        res = NexusDownloader(self._api).download_file(
            entry["game_domain"], entry["mod_id"], entry["file_id"],
            dest_dir=get_download_cache_dir_for_game(self._game_name),
            progress_cb=_cb, cancel=self._cancel_evt,
            known_file_name=entry.get("file_name", ""),
            expected_size_bytes=entry.get("size_bytes", 0))
        if not (res.success and res.file_path):
            if res.error:
                self._log(f"[export] archive redownload failed for "
                          f"'{entry['name']}': {res.error}")
            return False
        # A cache-dir hit (name matched, sidecar missing) returns the file
        # without stamping it - stamp here so _cached_archive finds it by id.
        _write_sidecar_file_id(Path(res.file_path), entry["file_id"])
        return True

    def _fetch_manual(self, entry: dict, idx: int, total: int) -> bool:
        """Free account: open the file's download page and watch the download
        folders; the finished archive is copied into the game cache."""
        from Nexus.manual_download_watch import (
            ManualDownloadWatcher, find_existing_archive)
        from Nexus.nexus_download import ingest_archive_to_cache

        mod_id, file_id = entry["mod_id"], entry["file_id"]
        try:
            finfo = self._api.get_file_info(
                entry["game_domain"], mod_id, file_id)
        except Exception as exc:
            self._log(f"[export] file lookup failed for "
                      f"'{entry['name']}': {exc}")
            return False
        files = [finfo]

        hit = find_existing_archive(mod_id, files)
        if hit is not None:
            return ingest_archive_to_cache(
                hit[0], self._game_name, file_id) is not None

        url = (f"https://www.nexusmods.com/{entry['game_domain']}/mods/"
               f"{mod_id}?tab=files&file_id={file_id}")
        safe_emit(self._manual_page, url)
        safe_emit(self._status, self._tpl_waiting.format(
            idx + 1, total, entry["name"]))
        try:
            from Utils.environment.xdg import open_url
            open_url(url)
        except Exception:
            pass

        found = {"path": None}
        done_evt = threading.Event()

        def _found(path, _file):
            found["path"] = path
            done_evt.set()

        def _progress(done, tot):
            safe_emit(self._status, self._tpl_waiting_prog.format(
                idx + 1, total, entry["name"], _fmt_mb(done), _fmt_mb(tot)))

        self._skip_evt.clear()
        watcher = ManualDownloadWatcher(
            mod_id=mod_id, files=files, on_found=_found,
            on_progress=_progress, on_timeout=done_evt.set)
        self._watcher = watcher
        watcher.start()
        try:
            while not done_evt.wait(0.25):
                if self._cancel_evt.is_set() or self._skip_evt.is_set():
                    watcher.stop()
                    break
        finally:
            self._watcher = None
            self._skip_evt.clear()
            safe_emit(self._manual_page, "")
        if found["path"] is None:
            return False
        return ingest_archive_to_cache(
            found["path"], self._game_name, file_id) is not None

    # -- UI-thread slots -----------------------------------------------------
    def _on_status(self, text: str):
        self._status_lbl.setText(text or "")

    def _on_item_state(self, idx: int, state: str):
        if not (0 <= idx < self._list.count()):
            return
        suffix = {"ok": self.tr(" - downloaded ✓"),
                  "failed": self.tr(" - failed ✗")}.get(state, "")
        self._list.item(idx).setText(self._item_base[idx] + suffix)

    def _on_manual_page(self, url: str):
        self._manual_url = url or ""
        for b in (self._open_btn, self._skip_btn):
            b.setVisible(bool(url))

    def _open_manual_page(self):
        if self._manual_url:
            from Utils.environment.xdg import open_url
            open_url(self._manual_url)

    def _on_fetch_done(self, fetched: int, failed: int):
        self._fetching = False
        if failed == 0:
            self._finish(True)
            return
        self._status_lbl.setText(self.tr(
            "{0} archive(s) downloaded, {1} could not be fetched - their "
            "edits or installer choices will be left out.").format(
                fetched, failed))
        self._continue_btn.setEnabled(True)
        self._continue_btn.setText(self.tr("Continue anyway"))
        self._dl_btn.setEnabled(True)
        self._dl_btn.setText(self.tr("Retry"))
