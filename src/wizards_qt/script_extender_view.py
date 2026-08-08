"""Script Extender installer - Qt port of wizards/script_extender.py.

One view serves every registration (SKSE64, F4SE, NVSE, FOSE, SFSE, xOBSE,
SKSE VR …), parameterized via WizardTool.extra:
  github_api_url      - auto-fetch the latest release from the GitHub API
  direct_download_url - direct archive URL (no page)
  download_url        - manual fallback page opened in the browser
  archive_keywords    - substrings to match the asset / Downloads archive
  versions            - optional list of {label, description, github_api_url,
                        direct_download_url, download_url, archive_keywords}
                        shown as a pick-one first step (Skyrim SE's 3 builds)

Flow: [version select] → download (auto with progress, OR manual page +
locate-in-Downloads) → extract to game root (restore-to-vanilla first) /
Root_Folder / managed root-flagged mod. All heavy work runs on daemon threads
with Signals; download/locate/extract logic lives in Utils.wizard_archives.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QStackedWidget,
    QRadioButton, QButtonGroup, QProgressBar, QFrame,
)

from gui_qt.theme_qt import active_palette, _c, button_qss, ok_text, err_text
from gui_qt.safe_emit import safe_emit
from Utils.wizard_archives import (
    fetch_latest_github_asset, find_archive, get_downloads_dir,
    install_archive_payload,
)

if TYPE_CHECKING:
    from Games.base_game import BaseGame



class ScriptExtenderView(QWidget):
    """Download + install a script extender (or similar loose-loader tool)."""

    # Worker / portal → UI thread.
    _dl_status_sig = Signal(str, str)     # (text, color)
    _dl_progress_sig = Signal(int)        # percent 0-100
    _dl_done_sig = Signal(bool)           # download finished (ok?)
    _picked_sig = Signal(object)          # Path | None from the file portal
    _ex_status_sig = Signal(str, str)
    _ex_done_sig = Signal(bool)

    def __init__(self, game: "BaseGame", log_fn=None, on_close=None, ctx=None,
                 github_api_url: str = "", download_url: str = "",
                 archive_keywords: list | None = None,
                 direct_download_url: str = "", versions: list | None = None):
        super().__init__()
        self._game = game
        self._log = log_fn or (lambda _m: None)
        self._on_close_cb = on_close or (lambda: None)
        self._ctx = ctx

        self._github_api_url = github_api_url or ""
        self._fallback_download_url = download_url or ""
        self._direct_download_url = direct_download_url or ""
        self._archive_keywords = [k.lower() for k in (archive_keywords or [])]
        self._versions = list(versions or [])

        self._archive_path: Path | None = None
        self._install_ok = False       # extraction succeeded
        self._installed_mode = ""      # mode of the successful install
        self._closing = False          # teardown started - drop late signals

        def _guard(fn):
            return lambda *a: None if self._closing else fn(*a)

        for sig, slot in (
            (self._dl_status_sig, lambda t, c: self._set_lbl(self._dl_status, t, c)),
            (self._dl_progress_sig, self._on_dl_progress),
            (self._dl_done_sig, self._on_dl_done),
            (self._picked_sig, self._on_file_picked),
            (self._ex_status_sig, lambda t, c: self._set_lbl(self._ex_status, t, c)),
            (self._ex_done_sig, self._on_extract_done),
        ):
            sig.connect(_guard(slot))

        self.setObjectName("ScriptExtenderView")
        self._build()
        # Route to the first page: version selector when configured, else the
        # download page that matches the resolved URLs.
        if self._versions:
            self._stack.setCurrentIndex(self._PG_VERSIONS)
        else:
            self._enter_download()

    # ---- scaffolding --------------------------------------------------------
    _PG_VERSIONS, _PG_DL_AUTO, _PG_DL_MANUAL, _PG_LOCATE, _PG_EXTRACT = range(5)

    def _build(self):
        p = active_palette()
        self._dim = f"color:{_c(p,'TEXT_DIM')};"
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)

        bar = QWidget(); bar.setObjectName("HeaderBar")
        hb = QHBoxLayout(bar); hb.setContentsMargins(12, 8, 8, 8); hb.setSpacing(8)
        title = QLabel(self.tr("Install Script Extender - {0}").format(self._game.name))
        title.setStyleSheet(f"color:{_c(p,'TEXT_MAIN')}; font-weight:600;")
        hb.addWidget(title)
        hb.addStretch(1)
        close = QPushButton(self.tr("✕ Close"))
        close.setCursor(Qt.PointingHandCursor)
        close.setStyleSheet(
            button_qss("BTN_DANGER", padding="5px 12px"))
        close.clicked.connect(self._finish)
        hb.addWidget(close)
        v.addWidget(bar)

        self._stack = QStackedWidget()
        v.addWidget(self._stack, 1)
        self._stack.addWidget(self._build_page_versions())   # 0
        self._stack.addWidget(self._build_page_dl_auto())    # 1
        self._stack.addWidget(self._build_page_dl_manual())  # 2
        self._stack.addWidget(self._build_page_locate())     # 3
        self._stack.addWidget(self._build_page_extract())    # 4

    def _page(self, title: str) -> tuple[QWidget, QVBoxLayout]:
        p = active_palette()
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(20, 16, 20, 16)
        lay.setSpacing(8)
        head = QLabel(title)
        head.setAlignment(Qt.AlignHCenter)
        head.setStyleSheet(f"color:{_c(p,'TEXT_MAIN')}; font-weight:700;")
        lay.addWidget(head)
        return w, lay

    def _status_label(self, lay: QVBoxLayout) -> QLabel:
        lbl = QLabel("")
        lbl.setAlignment(Qt.AlignHCenter)
        lbl.setWordWrap(True)
        lbl.setStyleSheet(self._dim)
        lay.addWidget(lbl)
        return lbl

    def _set_lbl(self, lbl: QLabel, text: str, color: str):
        lbl.setStyleSheet(f"color:{color};" if color else self._dim)
        lbl.setText(text)

    def _primary(self, text: str) -> QPushButton:
        b = QPushButton(text)
        b.setCursor(Qt.PointingHandCursor)
        b.setStyleSheet(
            button_qss("BTN_INFO"))
        return b

    def _mode_box(self) -> tuple[QWidget, QButtonGroup]:
        """Install-destination radios (shared look across both download pages)."""
        p = active_palette()
        box = QFrame()
        box.setStyleSheet(f"QFrame{{background:{_c(p,'BG_PANEL')}; border-radius:6px;}}")
        bv = QVBoxLayout(box); bv.setContentsMargins(12, 10, 12, 10); bv.setSpacing(4)
        head = QLabel(self.tr("Install destination"))
        head.setStyleSheet(f"color:{_c(p,'TEXT_MAIN')}; font-weight:600;")
        bv.addWidget(head)
        group = QButtonGroup(self)
        for val, label in (("game", self.tr("Game folder (restores to vanilla first)")),
                           ("root", self.tr("Root_Folder (staging)")),
                           ("mod", self.tr("As a managed mod (root-flagged)"))):
            rb = QRadioButton(label)
            rb.setProperty("mode", val)
            if val == "game":
                rb.setChecked(True)
            group.addButton(rb)
            bv.addWidget(rb)
        return box, group

    def _selected_mode(self) -> str:
        # Only one download page is visited per run; _enter_download records
        # which one, so we read the radios the user actually saw (the locate
        # page inherits the manual page's choice).
        group = getattr(self, "_active_mode_group", None)
        btn = group.checkedButton() if group is not None else None
        return btn.property("mode") if btn is not None else "game"

    # ---- page 0: version select ----------------------------------------------
    def _build_page_versions(self) -> QWidget:
        page, lay = self._page(self.tr("Choose a Version"))
        note = QLabel(self.tr("Multiple builds are available for this game - pick the "
                      "one that matches your game version."))
        note.setWordWrap(True); note.setAlignment(Qt.AlignHCenter)
        note.setStyleSheet(self._dim)
        lay.addWidget(note)
        p = active_palette()
        for ver in self._versions:
            card = QFrame()
            card.setStyleSheet(
                f"QFrame{{background:{_c(p,'BG_PANEL')}; border-radius:6px;}}")
            ch = QHBoxLayout(card); ch.setContentsMargins(12, 10, 12, 10); ch.setSpacing(10)
            text = QWidget()
            tv = QVBoxLayout(text); tv.setContentsMargins(0, 0, 0, 0); tv.setSpacing(2)
            name = QLabel(str(ver.get("label", "Version")))
            name.setStyleSheet(f"color:{_c(p,'TEXT_MAIN')}; font-weight:600;")
            tv.addWidget(name)
            desc = QLabel(str(ver.get("description", "")))
            desc.setWordWrap(True)
            desc.setStyleSheet(self._dim)
            tv.addWidget(desc)
            ch.addWidget(text, 1)
            btn = self._primary(self.tr("Select"))
            btn.clicked.connect(lambda _=False, vv=ver: self._on_version_selected(vv))
            ch.addWidget(btn)
            lay.addWidget(card)
        lay.addStretch(1)
        return page

    def _on_version_selected(self, ver: dict):
        self._github_api_url = ver.get("github_api_url", "") or ""
        self._direct_download_url = ver.get("direct_download_url", "") or ""
        self._fallback_download_url = ver.get("download_url", "") or ""
        kws = ver.get("archive_keywords") or []
        if kws:
            self._archive_keywords = [k.lower() for k in kws]
        self._log(f"Wizard: selected version '{ver.get('label', '?')}'.")
        self._enter_download()

    # ---- page 1: download (auto) ----------------------------------------------
    def _build_page_dl_auto(self) -> QWidget:
        page, lay = self._page(self.tr("Download Script Extender"))
        self._dl_status = self._status_label(lay)
        self._dl_bar = QProgressBar()
        self._dl_bar.setRange(0, 0)
        self._dl_bar.setTextVisible(False)
        lay.addWidget(self._dl_bar)
        box, self._auto_mode_group = self._mode_box()
        lay.addWidget(box)
        lay.addStretch(1)
        nav = QWidget(); nh = QHBoxLayout(nav); nh.setContentsMargins(0, 0, 0, 0); nh.setSpacing(8)
        nh.addStretch(1)
        browse = QPushButton(self.tr("Browse…"))
        browse.clicked.connect(self._browse_archive)
        nh.addWidget(browse)
        self._dl_next_btn = self._primary(self.tr("Next →"))
        self._dl_next_btn.setEnabled(False)
        self._dl_next_btn.clicked.connect(self._enter_extract)
        nh.addWidget(self._dl_next_btn)
        nh.addStretch(1)
        lay.addWidget(nav)
        return page

    def _enter_download(self):
        if self._github_api_url or self._direct_download_url:
            self._active_mode_group = self._auto_mode_group
            self._stack.setCurrentIndex(self._PG_DL_AUTO)
            self._start_download()
        else:
            self._active_mode_group = self._manual_mode_group
            self._stack.setCurrentIndex(self._PG_DL_MANUAL)

    def _start_download(self):
        self._dl_bar.setRange(0, 0)
        self._dl_bar.setVisible(True)
        self._dl_next_btn.setEnabled(False)
        api_url = self._github_api_url
        direct = self._direct_download_url
        keywords = list(self._archive_keywords)

        def worker():
            try:
                if api_url:
                    safe_emit(self._dl_status_sig,
                              self.tr("Fetching release from GitHub…"), "")
                    tag, url = fetch_latest_github_asset(api_url, keywords)
                elif direct:
                    tag, url = self.tr("selected version"), direct
                else:
                    raise RuntimeError(self.tr("No download URL configured."))
                filename = url.split("/")[-1]
                dest = get_downloads_dir() / filename
                safe_emit(self._dl_status_sig,
                          self.tr("Downloading {0}…").format(tag), "")
                self._log(f"Wizard: downloading {url} → {dest}")

                def _reporthook(block_num, block_size, total_size):
                    if total_size > 0:
                        pct = min(block_num * block_size * 100 // total_size, 100)
                        safe_emit(self._dl_progress_sig, int(pct))

                from Utils.ca_bundle import download_file
                download_file(url, dest, reporthook=_reporthook)
                self._archive_path = dest
                safe_emit(self._dl_status_sig,
                    self.tr("Downloaded {0}.\nChoose the install destination, "
                    "then click Next.").format(filename), ok_text())
                safe_emit(self._dl_done_sig, True)
            except Exception as exc:
                self._log(f"Wizard: download failed: {exc}")
                safe_emit(self._dl_status_sig,
                    self.tr("Download failed:\n{0}\n\nUse Browse… to pick an "
                    "archive you downloaded manually.").format(exc), err_text())
                safe_emit(self._dl_done_sig, False)

        threading.Thread(target=worker, daemon=True,
                         name="script-extender-dl").start()

    def _on_dl_progress(self, pct: int):
        if self._dl_bar.maximum() == 0:
            self._dl_bar.setRange(0, 100)
        self._dl_bar.setValue(pct)

    def _on_dl_done(self, ok: bool):
        self._dl_bar.setVisible(False)
        # Next is enabled either way: on failure the user can Browse… first
        # (Next only proceeds once an archive is set).
        self._dl_next_btn.setEnabled(self._archive_path is not None or not ok)

    # ---- page 2: download (manual) ----------------------------------------------
    def _build_page_dl_manual(self) -> QWidget:
        page, lay = self._page(self.tr("Download Script Extender"))
        note = QLabel(
            self.tr("This script extender must be downloaded manually. Click the "
            "button below to open the download page, save the archive to "
            "your Downloads folder, then click Next."))
        note.setWordWrap(True); note.setAlignment(Qt.AlignHCenter)
        note.setStyleSheet(self._dim)
        lay.addWidget(note)
        # Always built - with a `versions` config the URL is only known after
        # the user picks one, so the handler reads it live (no-op when empty).
        open_btn = self._primary(self.tr("Open Download Page"))
        open_btn.clicked.connect(self._open_download_page)
        lay.addWidget(open_btn, 0, Qt.AlignHCenter)
        box, self._manual_mode_group = self._mode_box()
        lay.addWidget(box)
        lay.addStretch(1)
        nxt = self._primary(self.tr("Next →"))
        nxt.clicked.connect(self._enter_locate)
        lay.addWidget(nxt, 0, Qt.AlignHCenter)
        return page

    def _open_download_page(self):
        url = self._fallback_download_url
        if not url:
            return
        from Utils.xdg import open_url
        open_url(url)
        self._log(f"Wizard: opened {url}")

    # ---- page 3: locate ----------------------------------------------------------
    def _build_page_locate(self) -> QWidget:
        page, lay = self._page(self.tr("Locate the Archive"))
        self._locate_status = self._status_label(lay)
        lay.addStretch(1)
        nav = QWidget(); nh = QHBoxLayout(nav); nh.setContentsMargins(0, 0, 0, 0); nh.setSpacing(8)
        nh.addStretch(1)
        retry = QPushButton(self.tr("Try Again"))
        retry.clicked.connect(self._scan_downloads)
        nh.addWidget(retry)
        browse = QPushButton(self.tr("Browse…"))
        browse.clicked.connect(self._browse_archive)
        nh.addWidget(browse)
        self._locate_next_btn = self._primary(self.tr("Next →"))
        self._locate_next_btn.setEnabled(False)
        self._locate_next_btn.clicked.connect(self._enter_extract)
        nh.addWidget(self._locate_next_btn)
        nh.addStretch(1)
        lay.addWidget(nav)
        return page

    def _enter_locate(self):
        self._stack.setCurrentIndex(self._PG_LOCATE)
        self._scan_downloads()

    def _scan_downloads(self):
        found = find_archive(get_downloads_dir(), self._archive_keywords)
        if found is not None:
            self._archive_path = found
            self._set_lbl(self._locate_status,
                          self.tr("Found: {0}\nClick Next to install it.").format(
                              found.name),
                          ok_text())
            self._locate_next_btn.setEnabled(True)
        else:
            kw = ", ".join(self._archive_keywords) or "?"
            self._set_lbl(self._locate_status,
                          self.tr("No archive matching '{0}' was found in your "
                          "Downloads folder.\nDownload it first, then Try "
                          "Again - or Browse… to pick the file.").format(kw),
                          err_text())
            self._locate_next_btn.setEnabled(False)

    # ---- browse (shared) -----------------------------------------------------------
    def _browse_archive(self):
        from Utils.portal_filechooser import pick_file
        # Portal callback fires on a WORKER thread - marshal via Signal.
        pick_file(self.tr("Select the script extender archive"),
                  lambda path: safe_emit(self._picked_sig, path))

    def _on_file_picked(self, path):
        if not path:
            return
        self._archive_path = Path(path)
        name = self._archive_path.name
        # Reflect the pick on whichever page is showing.
        idx = self._stack.currentIndex()
        if idx == self._PG_DL_AUTO:
            self._set_lbl(self._dl_status,
                          self.tr("Selected: {0}\nChoose the install destination, "
                          "then click Next.").format(name), ok_text())
            self._dl_next_btn.setEnabled(True)
        elif idx == self._PG_LOCATE:
            self._set_lbl(self._locate_status,
                          self.tr("Selected: {0}\nClick Next to install it.").format(
                              name), ok_text())
            self._locate_next_btn.setEnabled(True)

    # ---- page 4: extract ---------------------------------------------------------
    def _build_page_extract(self) -> QWidget:
        page, lay = self._page(self.tr("Install Script Extender"))
        self._ex_status = self._status_label(lay)
        lay.addStretch(1)
        self._done_btn = QPushButton(self.tr("Done"))
        self._done_btn.setEnabled(False)
        self._done_btn.setCursor(Qt.PointingHandCursor)
        self._done_btn.setStyleSheet(
            button_qss("BTN_SUCCESS"))
        self._done_btn.clicked.connect(self._finish)
        lay.addWidget(self._done_btn, 0, Qt.AlignHCenter)
        return page

    def _enter_extract(self):
        if self._archive_path is None or not self._archive_path.is_file():
            return
        mode = self._selected_mode()
        self._installed_mode = mode
        self._stack.setCurrentIndex(self._PG_EXTRACT)
        self._set_lbl(self._ex_status, self.tr("Extracting…"), "")
        game = self._game
        archive = self._archive_path

        def worker():
            try:
                if mode == "game":
                    safe_emit(self._ex_status_sig,
                        self.tr("Restoring game to vanilla state…"), "")
                dest_label, file_count, _mod = install_archive_payload(
                    game, archive, mode,
                    mod_fallback_name="Script Extender",
                    log_fn=lambda m: self._log(str(m)))
                safe_emit(self._ex_status_sig,
                    self.tr("Script extender installed successfully!\n"
                    "{0} file(s) extracted to the {1}.\n\n"
                    "Click Done to close.").format(file_count, dest_label),
                    ok_text())
                safe_emit(self._ex_done_sig, True)
            except Exception as exc:
                self._log(f"Wizard error: {exc}")
                safe_emit(self._ex_status_sig,
                          self.tr("Error: {0}").format(exc), err_text())
                safe_emit(self._ex_done_sig, False)

        threading.Thread(target=worker, daemon=True,
                         name="script-extender-extract").start()

    def _on_extract_done(self, ok: bool):
        self._done_btn.setEnabled(True)
        if ok:
            self._install_ok = True
            # A managed-mod install changed modlist.txt - reload it now, on
            # the GUI thread (never from the worker).
            if self._installed_mode == "mod":
                refresh = getattr(self._ctx, "refresh_modlist", None)
                if refresh is not None:
                    refresh()

    # ---- teardown ---------------------------------------------------------------
    def _finish(self):
        if self._closing:
            return
        self._closing = True
        self._on_close_cb()
