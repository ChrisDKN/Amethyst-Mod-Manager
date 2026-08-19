"""Shared shell + page builders for Qt wizard views.

Every wizard view repeats the same skeleton (header bar with ✕, a
QStackedWidget of step pages, worker→UI Signals guarded by `_closing`,
an idempotent `_finish` that optionally refreshes the modlist) and most of
the download-family wizards repeat the same three pages (manual-download →
locate-in-Downloads → extract).  This base captures that skeleton so each
view only writes its tool-specific pages and workers.

Standards baked in (see memory feedback notes):
  * every worker/callback emit goes through safe_emit;
  * the portal file-picker callback fires on a WORKER thread → marshalled
    via `_picked_sig`;
  * ✕ / Done / auto-close all land in the idempotent `_finish()`; late
    worker signals are dropped by the `_closing` guards;
  * `ctx.refresh_modlist` runs on the GUI thread only, and only when the
    view marked `_ran = True`;
  * no hardcoded font-size - headers use font-weight only.
"""

from __future__ import annotations

import re
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtCore import Qt, QCoreApplication, QTimer, Signal
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QStackedWidget,
)

from gui_qt.safe_emit import safe_emit
from gui_qt.theme_qt import (active_palette, _c, button_qss, ok_text, err_text,
                             warn_text)

if TYPE_CHECKING:
    from Games.base_game import BaseGame

# Back-compat status-text colours re-exported for the many wizard views that do
# ``from wizards_qt._view_base import GREEN, RED``. Prefer ok_text()/err_text()
# in new code - these module constants resolve once at import from the active
# theme, which is fine for wizards (created after the theme is applied).
GREEN = ok_text()
RED = err_text()
AMBER = warn_text()


def parse_nexus_mod_url(url: str) -> "tuple[str, int] | None":
    """Extract (game_domain, mod_id) from a nexusmods.com mod page URL."""
    m = re.search(r"nexusmods\.com/([^/?#]+)/mods/(\d+)", url or "")
    if m is None:
        return None
    return m.group(1), int(m.group(2))


def arm_nexus_auto_fetch(*, api, url: str, file_id: int, keywords: list[str],
                         label: str, cancel: threading.Event,
                         status_cb, gate_cb, archive_cb, log_fn) -> bool:
    """Hands-free archive acquire for a wizard's manual-download step.

    Premium accounts download the pinned (mod_id-from-*url*, *file_id*)
    straight through the Nexus API; everyone else keeps the manual page while
    the download folders are watched, so the wizard advances the moment the
    browser download completes (mirrors the ESM Fixes / BSA Decompressor
    hands-free flow in Utils.mpi_auto_fetch).

    All callbacks fire on the WORKER thread - pass safe_emit lambdas:
    ``status_cb(text, color)`` updates the download-page status,
    ``gate_cb(enabled)`` gates the page's manual Next button (disabled while
    a premium download streams, so a partial file can't be picked up by the
    locate scan), ``archive_cb(path)`` delivers the finished archive.
    Returns False when *url* has no parseable mod page / *file_id* is unset.
    """
    parsed = parse_nexus_mod_url(url)
    if parsed is None or not file_id:
        return False
    game_domain, mod_id = parsed
    from Utils.mpi_auto_fetch import start_auto_fetch
    from Utils.wizard_archives import find_archive, get_downloads_dir
    # NOTE: call QCoreApplication.translate() spelled out - pyside6-lupdate
    # matches that literal name. Behind a local alias it falls back to the
    # QObject.tr(source, disambiguation) signature and mis-extracts the
    # context as the source string.
    last_pct = [-1]
    armed_at = time.time()

    def _find():
        # Watch-mode fallback (no API): only accept archives that appeared
        # after the wizard opened - a stale keyword match in Downloads must
        # not hijack the flow (the locate page still offers it manually).
        p = find_archive(get_downloads_dir(), list(keywords))
        if p is None:
            return None
        try:
            if p.stat().st_mtime < armed_at - 5:
                return None
        except OSError:
            return None
        return p

    def _progress(done, total):
        if total <= 0:
            return
        pct = min(100, int(done * 100 / total))
        if pct == last_pct[0]:
            return
        last_pct[0] = pct
        status_cb(QCoreApplication.translate(
            "WizardViewBase",
            "Downloading {0} from Nexus… {1}%").format(label, pct), "")

    def _started():
        gate_cb(False)
        status_cb(QCoreApplication.translate(
            "WizardViewBase",
            "Premium account - downloading {0} from Nexus…").format(label), "")

    def _waiting():
        gate_cb(True)
        status_cb(QCoreApplication.translate(
            "WizardViewBase",
            "The archive is picked up automatically once the "
            "download finishes."), "")

    start_auto_fetch(
        api=api,
        game_domain=game_domain,
        mod_id=mod_id,
        file_id=file_id,
        find_archive_fn=_find,
        on_archive=archive_cb,
        cancel=cancel,
        label=label,
        on_download_started=_started,
        on_progress=_progress,
        on_waiting=_waiting,
        log_fn=log_fn)
    return True


class WizardViewBase(QWidget):
    """Skeleton for a wizard view: header bar + step stack + teardown."""

    # Generic worker→UI signals shared by the common pages. Subclasses add
    # their own as needed; connect everything through self._guard().
    _locate_status_sig = Signal(str, str)
    _extract_status_sig = Signal(str, str)
    _run_status_sig = Signal(str, str)
    _picked_sig = Signal(object)          # portal picker → UI thread
    _extract_done_sig = Signal(bool)
    _run_started_sig = Signal()           # tool launched → enable Done
    _run_finished_sig = Signal()          # worker chain complete → close
    _auto_dl_status_sig = Signal(str, str)   # auto-fetch → download-page status
    _auto_dl_gate_sig = Signal(bool)         # auto-fetch → manual Next enable
    _auto_dl_archive_sig = Signal(object)    # auto-fetch → finished archive

    def __init__(self, game: "BaseGame", log_fn=None, on_close=None, ctx=None,
                 *, title: str, show_header: bool = True):
        super().__init__()
        self._game = game
        self._log = log_fn or (lambda _m: None)
        self._on_close_cb = on_close or (lambda: None)
        self._ctx = ctx
        self._archive_path: Path | None = None
        self._ran = False          # set True when refresh-worthy work happened
        self._closing = False
        self._auto_fetch_cancel = threading.Event()
        self._auto_fetch_started = False
        self._auto_fetch_pages: tuple[int, ...] = ()
        self._auto_fetch_on_archive = None
        self._dl_status: QLabel | None = None
        self._dl_next_btn: QPushButton | None = None

        self._locate_status_sig.connect(self._guard(
            lambda t, c: self._set_status(self._locate_status, t, c)))
        self._extract_status_sig.connect(self._guard(
            lambda t, c: self._set_status(self._extract_status, t, c)))
        self._run_status_sig.connect(self._guard(
            lambda t, c: self._set_status(self._run_status, t, c)))
        self._picked_sig.connect(self._guard(self._on_picked))
        self._extract_done_sig.connect(self._guard(self._on_extract_done))
        self._run_started_sig.connect(self._guard(self._on_run_started))
        self._run_finished_sig.connect(self._guard(self._finish))
        self._auto_dl_status_sig.connect(self._guard(self._on_auto_dl_status))
        self._auto_dl_gate_sig.connect(self._guard(self._on_auto_dl_gate))
        self._auto_dl_archive_sig.connect(self._guard(self._on_auto_dl_archive))

        p = active_palette()
        self._dim = f"color:{_c(p,'TEXT_DIM')};"
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)

        # show_header=False embeds this view inside another wizard's page -
        # the host provides the title bar / close, so skip building ours.
        if show_header:
            bar = QWidget(); bar.setObjectName("HeaderBar")
            hb = QHBoxLayout(bar); hb.setContentsMargins(12, 8, 8, 8); hb.setSpacing(8)
            head = QLabel(title)
            head.setStyleSheet(f"color:{_c(p,'TEXT_MAIN')}; font-weight:600;")
            hb.addWidget(head)
            hb.addStretch(1)
            close = QPushButton(self.tr("✕ Close"))
            close.setCursor(Qt.PointingHandCursor)
            close.setStyleSheet(button_qss("BTN_DANGER", pal=p, padding="5px 12px"))
            close.clicked.connect(self._finish)
            hb.addWidget(close)
            v.addWidget(bar)

        self._stack = QStackedWidget()
        v.addWidget(self._stack, 1)

    # ---- guard / teardown -----------------------------------------------------
    def _guard(self, fn):
        return lambda *a: None if self._closing else fn(*a)

    def _finish(self):
        # ✕, Done, and any auto-close all land here. Idempotent; in-flight
        # daemon workers finish harmlessly (late signals dropped by guards,
        # late emits dropped by safe_emit).
        if self._closing:
            return
        self._closing = True
        self._auto_fetch_cancel.set()
        do_refresh = self._ran and getattr(self._ctx, "refresh_modlist", None)
        self._on_close_cb()
        if do_refresh:
            do_refresh()

    # ---- widget helpers ---------------------------------------------------------
    def _step_page(self, title: str) -> tuple[QWidget, QVBoxLayout]:
        p = active_palette()
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(20, 16, 20, 16)
        lay.setSpacing(8)
        head = QLabel(title)
        head.setAlignment(Qt.AlignHCenter)
        head.setStyleSheet(f"color:{_c(p,'TEXT_MAIN')}; font-weight:700;")
        lay.addWidget(head)
        return page, lay

    def _make_note(self, lay: QVBoxLayout, text: str) -> QLabel:
        note = QLabel(text)
        note.setAlignment(Qt.AlignHCenter)
        note.setWordWrap(True)
        note.setStyleSheet(self._dim)
        lay.addWidget(note)
        return note

    def _make_status(self, lay: QVBoxLayout) -> QLabel:
        lbl = QLabel("")
        lbl.setAlignment(Qt.AlignHCenter)
        lbl.setWordWrap(True)
        lbl.setStyleSheet(self._dim)
        lay.addWidget(lbl)
        return lbl

    def _set_status(self, lbl: QLabel, text: str, color: str = ""):
        lbl.setStyleSheet(f"color:{color};" if color else self._dim)
        lbl.setText(text)

    def _accent_btn(self, text: str) -> QPushButton:
        b = QPushButton(text)
        b.setCursor(Qt.PointingHandCursor)
        b.setStyleSheet(button_qss("BTN_INFO"))
        return b

    def _green_btn(self, text: str | None = None) -> QPushButton:
        if text is None:
            text = self.tr("Done")
        b = QPushButton(text)
        b.setCursor(Qt.PointingHandCursor)
        b.setStyleSheet(button_qss("BTN_SUCCESS"))
        return b

    def _orange_btn(self, text: str) -> QPushButton:
        b = QPushButton(text)
        b.setCursor(Qt.PointingHandCursor)
        b.setStyleSheet(button_qss("BTN_WARN"))
        return b

    def _red_btn(self, text: str) -> QPushButton:
        b = QPushButton(text)
        b.setCursor(Qt.PointingHandCursor)
        b.setStyleSheet(button_qss("BTN_DANGER"))
        return b

    # ---- common page: manual download -------------------------------------------
    def _build_manual_download_page(self, heading: str, note: str,
                                    url: str, on_next,
                                    button_text: str | None = None,
                                    next_text: str | None = None) -> QWidget:
        if button_text is None:
            button_text = self.tr("Open Download Page")
        if next_text is None:
            next_text = self.tr("Next →")
        page, lay = self._step_page(heading)
        self._make_note(lay, note)
        lay.addSpacing(8)
        open_btn = self._orange_btn(button_text)
        open_btn.clicked.connect(lambda: self._open_url(url))
        lay.addWidget(open_btn, 0, Qt.AlignHCenter)
        lay.addSpacing(8)
        self._dl_status = self._make_status(lay)
        lay.addStretch(1)
        nxt = self._accent_btn(next_text)
        nxt.clicked.connect(on_next)
        lay.addWidget(nxt, 0, Qt.AlignHCenter)
        self._dl_next_btn = nxt
        return page

    def _open_url(self, url: str):
        from Utils.xdg import open_url
        open_url(url)

    # ---- hands-free archive fetch for the manual-download page -------------------
    def _nexus_auto_fetch(self, *, url: str, file_id: int,
                          keywords: list[str], label: str,
                          pages: tuple[int, ...], on_archive):
        """Arm the premium-download / folder-watch acquire for the wizard's
        pinned Nexus file. Call when the manual-download page is shown.
        *pages* are the stack indices (download / locate) where the fetched
        archive may still auto-advance the wizard; *on_archive(path)* runs on
        the GUI thread with self._archive_path already set."""
        if self._auto_fetch_started or self._closing:
            return
        api = None
        api_fn = getattr(self._ctx, "nexus_api", None)
        if api_fn is not None:
            try:
                api = api_fn()      # GUI thread - the app's shared resolver
            except Exception:
                api = None
        self._auto_fetch_pages = tuple(pages)
        self._auto_fetch_on_archive = on_archive
        self._auto_fetch_started = arm_nexus_auto_fetch(
            api=api, url=url, file_id=file_id, keywords=keywords,
            label=label, cancel=self._auto_fetch_cancel,
            status_cb=lambda t, c: safe_emit(self._auto_dl_status_sig, t, c),
            gate_cb=lambda on: safe_emit(self._auto_dl_gate_sig, on),
            archive_cb=lambda p: safe_emit(self._auto_dl_archive_sig, p),
            log_fn=lambda m: self._log(f"{label} Wizard: {m}"))

    def _on_auto_dl_status(self, text: str, color: str):
        if self._dl_status is not None:
            self._set_status(self._dl_status, text, color)

    def _on_auto_dl_gate(self, enabled: bool):
        if self._dl_next_btn is not None:
            self._dl_next_btn.setEnabled(enabled)

    def _on_auto_dl_archive(self, path):
        self._on_auto_dl_gate(True)
        path = Path(path)
        # Only auto-advance while still on the download/locate steps - a user
        # who already located an archive manually keeps their choice.
        if self._stack.currentIndex() not in self._auto_fetch_pages:
            return
        if not path.is_file():
            return
        self._archive_path = path
        if self._dl_status is not None:
            self._set_status(self._dl_status,
                             self.tr("Downloaded: {0}").format(path.name),
                             ok_text())
        if self._auto_fetch_on_archive is not None:
            self._auto_fetch_on_archive(path)

    # ---- common page: locate archive in ~/Downloads ------------------------------
    def _build_locate_page(self, heading: str | None = None,
                           *, with_next: bool = False) -> QWidget:
        """Creates self._locate_status (+ optional gated self._locate_next_btn
        instead of auto-advance). Configure the scan with _enter_locate()."""
        if heading is None:
            heading = self.tr("Locate the Archive")
        page, lay = self._step_page(heading)
        self._locate_page = page
        self._locate_status = self._make_status(lay)
        lay.addStretch(1)
        row = QWidget()
        rh = QHBoxLayout(row); rh.setContentsMargins(0, 8, 0, 0); rh.setSpacing(8)
        rh.addStretch(1)
        browse = QPushButton(self.tr("Browse…"))
        browse.setCursor(Qt.PointingHandCursor)
        browse.clicked.connect(self._browse_archive)
        rh.addWidget(browse)
        retry = QPushButton(self.tr("Try Again"))
        retry.setCursor(Qt.PointingHandCursor)
        retry.clicked.connect(self._locate_rescan)
        rh.addWidget(retry)
        self._locate_next_btn = None
        if with_next:
            self._locate_next_btn = self._accent_btn(self.tr("Next →"))
            self._locate_next_btn.setEnabled(False)
            self._locate_next_btn.clicked.connect(
                lambda: self._locate_on_ready(self._archive_path))
            rh.addWidget(self._locate_next_btn)
        rh.addStretch(1)
        lay.addWidget(row)
        return page

    def _enter_locate(self, keywords: list[str], pick_title: str,
                      not_found_text: str, on_ready):
        """Call when routing to the locate page: scans Downloads for an
        archive matching all *keywords*; on_ready(path) fires (auto after
        300ms, or via Next when the page was built with_next=True)."""
        self._locate_keywords = keywords
        self._locate_pick_title = pick_title
        self._locate_not_found = not_found_text
        self._locate_on_ready = on_ready
        self._set_status(self._locate_status, self.tr("Searching Downloads folder…"))
        self._locate_rescan()

    def _locate_rescan(self):
        from Utils.wizard_archives import find_archive, get_downloads_dir
        found = find_archive(get_downloads_dir(), self._locate_keywords)
        if found:
            self._archive_found(found, self.tr("Found: {0}").format(found.name))
        else:
            self._archive_path = None
            if self._locate_next_btn is not None:
                self._locate_next_btn.setEnabled(False)
            self._set_status(self._locate_status, self._locate_not_found, err_text())

    def _browse_archive(self):
        from Utils.portal_filechooser import pick_file
        # Portal callback fires on a WORKER thread - marshal via Signal.
        pick_file(self._locate_pick_title,
                  lambda p: safe_emit(self._picked_sig, p))

    def _on_picked(self, path):
        if path and Path(path).is_file():
            self._archive_found(
                Path(path),
                self.tr("Selected: {0}").format(Path(path).name))

    def _archive_found(self, path: Path, label: str):
        self._archive_path = path
        self._set_status(self._locate_status, label, ok_text())
        if self._locate_next_btn is not None:
            self._locate_next_btn.setEnabled(True)
        else:
            # Fire only while still on the locate page - the hands-free fetch
            # may have advanced the wizard while this timer was pending.
            QTimer.singleShot(300, self._guard(
                lambda: self._locate_on_ready(path)
                if self._stack.currentWidget() is self._locate_page else None))

    # ---- common page: extract status ---------------------------------------------
    def _build_extract_page(self, heading: str) -> QWidget:
        page, lay = self._step_page(heading)
        self._extract_status = self._make_status(lay)
        lay.addStretch(1)
        return page

    def _extract_to_applications(self, app_dir: str, exe_name: str,
                                 tool_label: str, *, marker: str = ""):
        """Worker: extract self._archive_path into Applications/<app_dir>,
        flatten wrappers until *exe_name* (or *marker* dir when given) is
        top-level, verify it exists and store the exe as self._exe.  Emits
        _extract_status_sig + _extract_done_sig.

        marker="" verifies the exe file; a *marker* (e.g. "tools") verifies a
        top-level directory instead (VRAMr/BENDr/ParallaxR ship a tools/ dir,
        not a launcher exe)."""
        archive, game = self._archive_path, self._game
        self._set_status(self._extract_status, self.tr("Extracting…"))

        def worker():
            import shutil
            from Utils.wizard_archives import extract_archive
            from Utils.xedit_tools import applications_dir, flatten_subdirs
            try:
                if archive is None or not archive.is_file():
                    raise RuntimeError(self.tr("Archive not found."))
                dest = applications_dir(game, app_dir)
                dest.mkdir(parents=True, exist_ok=True)
                safe_emit(self._extract_status_sig,
                          self.tr("Extracting {0}…").format(archive.name), "")
                self._log(f"{tool_label} Wizard: extracting {archive.name} → {dest}")
                paths = extract_archive(archive, dest)
                file_count = len([p for p in paths if p.is_file()])
                self._log(f"{tool_label} Wizard: extracted {file_count} file(s).")
                if marker:
                    # Collapse single-subdir wrappers until the marker dir is
                    # at the top level (ignoring loose files).
                    while True:
                        entries = [e for e in dest.iterdir()
                                   if e.name != "__MACOSX"]
                        subdirs = [e for e in entries if e.is_dir()]
                        if len(subdirs) == 1 and not (dest / marker).is_dir():
                            wrapper = subdirs[0]
                            tmp = dest.parent / (dest.name + "_flatten_tmp")
                            wrapper.rename(tmp)
                            for item in tmp.iterdir():
                                shutil.move(str(item), str(dest / item.name))
                            tmp.rmdir()
                        else:
                            break
                    if not (dest / marker).is_dir():
                        raise RuntimeError(self.tr(
                            "'{0}' folder not found after extraction.\n"
                            "Check that the archive contains {1}.").format(
                                marker, tool_label))
                    self._exe = dest / exe_name if exe_name else None
                else:
                    flatten_subdirs(dest, exe_name)
                    exe = dest / exe_name
                    if not exe.is_file():
                        raise RuntimeError(self.tr(
                            "{0} not found after extraction.\n"
                            "Check that the archive contains {0}.").format(
                                exe_name))
                    self._exe = exe
                safe_emit(self._extract_status_sig,
                          self.tr("Extracted {0} file(s).").format(file_count),
                          ok_text())
                safe_emit(self._extract_done_sig, True)
            except Exception as exc:
                safe_emit(self._extract_status_sig,
                          self.tr("Error: {0}").format(exc), err_text())
                self._log(f"{tool_label} Wizard: extract error: {exc}")
                safe_emit(self._extract_done_sig, False)

        threading.Thread(target=worker, daemon=True,
                         name="wizard-extract").start()

    # ---- common page: run (status + Done) -----------------------------------------
    def _build_run_page(self, heading: str) -> QWidget:
        page, lay = self._step_page(heading)
        self._run_status = self._make_status(lay)
        lay.addStretch(1)
        self._done_btn = self._green_btn(self.tr("Done"))
        self._done_btn.setEnabled(False)
        self._done_btn.clicked.connect(self._finish)
        lay.addWidget(self._done_btn, 0, Qt.AlignHCenter)
        return page

    # ---- worker: GitHub latest-release → Applications/<app_dir> --------------------
    def _github_install_worker(self, api_url: str, keywords: list[str],
                               app_dir: str, exe_name: str, tool_label: str,
                               status_sig, done_sig, progress_sig=None):
        """Fetch the newest matching GitHub release asset, download it,
        extract into Applications/<app_dir>, flatten wrappers and verify
        *exe_name*; stores the exe as self._exe.  Emits status_sig(str,str),
        done_sig(bool) and optional progress_sig(int 0-100)."""
        game = self._game

        def worker():
            import tempfile
            from Utils.ca_bundle import download_file
            from Utils.wizard_archives import (
                extract_archive, fetch_latest_github_asset,
            )
            from Utils.xedit_tools import applications_dir, flatten_subdirs
            try:
                safe_emit(status_sig,
                          self.tr("Fetching latest release from GitHub…"), "")
                tag, dl_url = fetch_latest_github_asset(api_url, keywords)
                safe_emit(status_sig,
                          self.tr("Downloading {0}…").format(tag), "")
                self._log(f"{tool_label} Wizard: downloading {tag} from {dl_url}")

                suffix = Path(dl_url).suffix or ".7z"
                with tempfile.NamedTemporaryFile(suffix=suffix,
                                                 delete=False) as tmp:
                    tmp_path = Path(tmp.name)
                hook = None
                if progress_sig is not None:
                    def hook(block_num, block_size, total_size):
                        if total_size > 0:
                            pct = min(100, block_num * block_size * 100 / total_size)
                            safe_emit(progress_sig, int(pct))
                download_file(dl_url, tmp_path, reporthook=hook)
                if progress_sig is not None:
                    safe_emit(progress_sig, 100)
                safe_emit(status_sig, self.tr("Extracting…"), "")

                dest = applications_dir(game, app_dir)
                dest.mkdir(parents=True, exist_ok=True)
                paths = extract_archive(tmp_path, dest)
                tmp_path.unlink(missing_ok=True)

                file_count = len([p for p in paths if p.is_file()])
                flatten_subdirs(dest, exe_name)
                if not (dest / exe_name).is_file():
                    raise RuntimeError(
                        self.tr("{0} not found after extraction.").format(
                            exe_name))
                self._exe = dest / exe_name

                self._log(f"{tool_label} Wizard: extracted {file_count} file(s).")
                safe_emit(status_sig,
                          self.tr("Downloaded and extracted {0}.").format(tag),
                          ok_text())
                safe_emit(done_sig, True)
            except Exception as exc:
                safe_emit(status_sig,
                          self.tr("Error: {0}").format(exc), err_text())
                self._log(f"{tool_label} Wizard: download error: {exc}")
                safe_emit(done_sig, False)

        threading.Thread(target=worker, daemon=True,
                         name="wizard-github-dl").start()

    # ---- common page: Proton step (built lazily on entry) --------------------------
    def _build_proton_holder(self) -> QWidget:
        self._proton_holder = QWidget()
        QVBoxLayout(self._proton_holder).setContentsMargins(0, 0, 0, 0)
        return self._proton_holder

    def _enter_proton(self, exe, exe_name: str, display_name: str, on_chosen,
                      *, allow_game_prefix: bool = True,
                      isolated_prefix_dir_fn=None,
                      default_prefix_mode: str | None = None,
                      title: str | None = None,
                      missing_text: str = ""):
        """(Re)build the Proton step on entry - the exe may only exist after
        an earlier extract step. on_chosen(proton_name, prefix_mode)."""
        if title is None:
            title = self.tr("Choose Proton Version")
        lay = self._proton_holder.layout()
        while lay.count():
            item = lay.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
        if exe is None:
            err = QLabel(missing_text or
                         self.tr("{0} was not found.\nReopen this wizard.")
                         .format(exe_name))
            err.setAlignment(Qt.AlignCenter)
            err.setWordWrap(True)
            err.setStyleSheet(f"color:{err_text()};")
            lay.addWidget(err)
            return
        from wizards_qt.proton_step import ProtonStepWidget
        lay.addWidget(ProtonStepWidget(
            self._game, exe, exe_name, display_name,
            on_continue=on_chosen,
            log_fn=self._log,
            allow_game_prefix=allow_game_prefix,
            isolated_prefix_dir_fn=isolated_prefix_dir_fn,
            default_prefix_mode=default_prefix_mode,
            title=title,
        ))

    # ---- deploy through the app machinery -------------------------------------------
    def _run_ctx_deploy(self, status_lbl: QLabel, on_ok, on_fail=None):
        """Start a deploy via ctx.run_deploy; done-callback fires on the UI
        thread. Returns False when no deploy hook is available."""
        run_deploy = getattr(self._ctx, "run_deploy", None)
        if run_deploy is None:
            self._set_status(status_lbl, self.tr("Deploy is unavailable here."), err_text())
            return False
        self._set_status(status_lbl, self.tr("Deploying…"))

        def _done(ok: bool):
            if self._closing:
                return
            if ok:
                self._set_status(status_lbl, self.tr("Deploy complete."), ok_text())
                on_ok()
            else:
                self._set_status(status_lbl, self.tr("Deploy failed - see log."), err_text())
                if on_fail is not None:
                    on_fail()

        if not run_deploy(_done):
            self._set_status(status_lbl, self.tr("Could not start deploy - see log."), err_text())
            if on_fail is not None:
                on_fail()
            return False
        return True

    # ---- restore through the app machinery ------------------------------------------
    def _run_ctx_restore(self, status_lbl: QLabel, on_ok, on_fail=None):
        """Start a modlist restore via ctx.run_restore; done-callback fires on
        the UI thread. Returns False when no restore hook is available or the
        restore could not start (e.g. a deploy is already running)."""
        run_restore = getattr(self._ctx, "run_restore", None)
        if run_restore is None:
            self._set_status(status_lbl, self.tr("Restore is unavailable here."), err_text())
            return False
        self._set_status(status_lbl, self.tr("Restoring modlist…"))

        def _done(ok: bool):
            if self._closing:
                return
            if ok:
                on_ok()
            else:
                self._set_status(status_lbl, self.tr("Restore failed - see log."), err_text())
                if on_fail is not None:
                    on_fail()

        if not run_restore(_done):
            self._set_status(status_lbl, self.tr("Could not start restore - see log."), err_text())
            if on_fail is not None:
                on_fail()
            return False
        return True

    # ---- common page: deploy (explicit Deploy + Skip buttons) -----------------------
    def _build_deploy_page(self, heading: str, note: str, on_next) -> QWidget:
        """Deploy step with explicit Deploy/Skip buttons; on_next() fires on
        Skip or after a successful deploy. Creates self._deploy_status."""
        page, lay = self._step_page(heading)
        if note:
            self._make_note(lay, note)
        self._deploy_status = self._make_status(lay)
        lay.addStretch(1)
        row = QWidget()
        rh = QHBoxLayout(row); rh.setContentsMargins(0, 8, 0, 0); rh.setSpacing(8)
        rh.addStretch(1)
        self._deploy_skip_btn = QPushButton(self.tr("Skip"))
        self._deploy_skip_btn.setCursor(Qt.PointingHandCursor)
        self._deploy_skip_btn.clicked.connect(lambda: on_next())
        rh.addWidget(self._deploy_skip_btn)
        self._deploy_btn = self._accent_btn(self.tr("Deploy"))
        self._deploy_btn.clicked.connect(lambda: self._start_deploy(on_next))
        rh.addWidget(self._deploy_btn)
        rh.addStretch(1)
        lay.addWidget(row)
        return page

    def _start_deploy(self, on_next):
        self._deploy_btn.setEnabled(False)
        self._deploy_skip_btn.setEnabled(False)

        def _re_enable():
            self._deploy_btn.setEnabled(True)
            self._deploy_skip_btn.setEnabled(True)

        if not self._run_ctx_deploy(self._deploy_status, on_next, _re_enable):
            _re_enable()

    # ---- hooks (override in subclasses) --------------------------------------------
    def _on_extract_done(self, ok: bool):
        pass

    def _on_run_started(self):
        pass
