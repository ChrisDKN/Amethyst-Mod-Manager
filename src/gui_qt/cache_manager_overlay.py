"""Cache manager - borderless in-window overlay (Qt port of Tk
gui/cache_manager_overlay.py).

A per-game download-cache browser: a scrollable list of each game's cache with
its size, plus (kept from the old Qt stub) a "leftover temp folders" row for
orphaned ``modmgr_*`` dirs. Select rows and Clear Selected / Clear All.

A dimmed borderless child of ``host.window()`` (NOT a top-level - gaming mode
opens top-levels behind the app), matching gui_qt/list_picker_overlay.py and
gui_qt/confirm_overlay.py. Size scans + clears run on daemon threads and marshal
back to the UI thread via Signals (workers never touch widgets).
"""

from __future__ import annotations

import threading

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QWidget, QFrame, QVBoxLayout, QHBoxLayout, QLabel, QCheckBox, QPushButton,
    QScrollArea, QSizePolicy,
)

from gui_qt.overlay_base import OverlayBase
from gui_qt.theme_qt import active_palette, _c
from gui_qt.confirm_overlay import ConfirmOverlay
from Utils.config_paths import get_download_cache_dir

# Sentinel key (in the checkbox / size-label dicts) for the orphaned-temp row -
# not a real per-game cache name, so it can't collide with one.
_ORPHANS = "\x00__orphans__"


class CacheManagerOverlay(OverlayBase):
    CARD_W = 560
    CARD_H = 560
    MIN_W = 360
    MIN_H = 300
    CLICK_OUTSIDE_CANCELS = True

    # worker -> UI thread (queued, thread-safe). Guard .emit() for a destroyed
    # widget (daemon threads outlive a quick close). Payloads typed `object`
    # so PySide6 marshals the plain dict/list across the thread boundary.
    _sizes_ready = Signal(object)        # {name: bytes} (per-game only)
    _orphans_ready = Signal(int, "qlonglong")   # dir count, total bytes
    _clear_done = Signal(int, object)    # cleared_count, errors

    def __init__(self, host: QWidget, active_game_name: str = "",
                 on_closed=None):
        super().__init__(host)
        self._on_closed = on_closed
        self._active = (active_game_name or "").strip()
        self._pal = active_palette()
        self._checks: dict[str, QCheckBox] = {}
        self._size_lbls: dict[str, QLabel] = {}
        self._sizes: dict[str, int] = {}     # key -> bytes, reused by Clear
        self._empty_lbl: QLabel | None = None
        self._orphan_scan_done = False
        self._total = 0

        self._sizes_ready.connect(self._on_sizes)
        self._orphans_ready.connect(self._on_orphans)
        self._clear_done.connect(self._on_clear_done)

        # Only style the card itself - the buttons inherit the global QSS
        # #DangerButton/#FormButton/#PrimaryButton rules (all with min-height:30
        # so the action-bar buttons stay the same size). A local #DangerButton
        # override here previously made "Clear All" a different height.
        _card, outer = self._make_card("CacheCard", margins=(0, 0, 0, 0),
                                       spacing=0, bg_key="BG_DEEP")

        self._build_toolbar(outer)
        self._build_header(outer)
        self._build_list(outer)
        self._build_actions(outer)

        self._present()
        # Even the cheap cache-root listdir can stall on a cold/network disk -
        # defer it a tick so the overlay paints instantly. Everything costlier
        # (sizes, the staging-root orphan sweep) runs on the scan thread.
        self._total_lbl.setText(self.tr("Total: calculating…"))
        QTimer.singleShot(0, self._populate)

    @classmethod
    def show_over(cls, host, active_game_name: str = "", on_closed=None):
        top = host.window() if host is not None else None
        return cls(top or host, active_game_name, on_closed)

    # ---- layout ------------------------------------------------------------
    def _build_toolbar(self, outer):
        p = self._pal
        bar = QFrame()
        bar.setObjectName("CacheToolbar")
        bar.setStyleSheet(f"#CacheToolbar {{ background:{_c(p,'BG_HEADER')};"
                          f" border-top-left-radius:8px;"
                          f" border-top-right-radius:8px; }}")
        h = QHBoxLayout(bar)
        h.setContentsMargins(12, 8, 8, 8)
        title = QLabel(self.tr("Manage Download Caches"))
        title.setStyleSheet(
            f"color:{_c(p,'TEXT_MAIN')}; font-weight:600; font-size:15px;")
        h.addWidget(title)
        h.addStretch(1)
        close = QPushButton(self.tr("✕ Close"))
        close.setObjectName("DangerButton")
        close.setCursor(Qt.PointingHandCursor)
        close.clicked.connect(self._finish)
        h.addWidget(close)
        outer.addWidget(bar)

    def _build_header(self, outer):
        p = self._pal
        wrap = QWidget()
        v = QVBoxLayout(wrap)
        v.setContentsMargins(12, 12, 12, 4)
        v.setSpacing(6)
        self._loc_lbl = QLabel(self.tr("Location: {0}").format(get_download_cache_dir()))
        self._loc_lbl.setStyleSheet(f"color:{_c(p,'TEXT_DIM')}; font-size:12px;")
        self._loc_lbl.setWordWrap(True)
        v.addWidget(self._loc_lbl)
        self._total_lbl = QLabel(self.tr("Total: calculating…"))
        self._total_lbl.setStyleSheet(
            f"color:{_c(p,'TEXT_MAIN')}; font-size:13px;")
        v.addWidget(self._total_lbl)
        outer.addWidget(wrap)

    def _build_list(self, outer):
        p = self._pal
        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QScrollArea.NoFrame)
        self._scroll.setStyleSheet(
            f"QScrollArea {{ background:{_c(p,'BG_PANEL')};"
            f" border:1px solid {_c(p,'BORDER')}; }}")
        self._rows_host = QWidget()
        self._rows_host.setStyleSheet(f"background:{_c(p,'BG_PANEL')};")
        self._rows_v = QVBoxLayout(self._rows_host)
        self._rows_v.setContentsMargins(0, 0, 0, 0)
        self._rows_v.setSpacing(1)
        self._rows_v.addStretch(1)
        self._scroll.setWidget(self._rows_host)
        wrap = QWidget()
        m = QVBoxLayout(wrap)
        m.setContentsMargins(12, 4, 12, 8)
        m.addWidget(self._scroll)
        outer.addWidget(wrap, 1)

    def _build_actions(self, outer):
        p = self._pal
        wrap = QWidget()
        v = QVBoxLayout(wrap)
        v.setContentsMargins(12, 0, 12, 12)
        v.setSpacing(6)
        self._status_lbl = QLabel("")
        self._status_lbl.setStyleSheet(
            f"color:{_c(p,'TEXT_DIM')}; font-size:12px;")
        self._status_lbl.setWordWrap(True)
        v.addWidget(self._status_lbl)

        row = QHBoxLayout()
        row.setSpacing(6)

        def _mk(text, obj, slot):
            b = QPushButton(text)
            b.setObjectName(obj)
            b.setCursor(Qt.PointingHandCursor)
            b.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
            b.clicked.connect(slot)
            row.addWidget(b)
            return b

        _mk(self.tr("All"), "FormButton", self._select_all)
        _mk(self.tr("None"), "FormButton", self._select_none)
        self._clear_sel_btn = _mk(self.tr("Clear Selected"), "PrimaryButton",
                                  self._on_clear_selected)
        self._clear_all_btn = _mk(self.tr("Clear All"), "DangerButton",
                                  self._on_clear_all)
        v.addLayout(row)
        outer.addWidget(wrap)

    # ---- row list ----------------------------------------------------------
    def _populate(self):
        """Rebuild the row list then kick off the async size scan. Runs off the
        constructor's paint (deferred) so the overlay appears instantly."""
        self._repaint()
        self._start_size_scan()

    def _repaint(self):
        """Per-game rows only - one listdir of the cache root, no tree walks.

        The leftover-temp row needs a sweep of every staging root, so it's
        appended later by :meth:`_on_orphans` off the scan thread.
        """
        from Utils.cache_tools import enumerate_game_caches
        # Clear existing rows (keep the trailing stretch).
        while self._rows_v.count() > 1:
            item = self._rows_v.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
        self._checks.clear()
        self._size_lbls.clear()
        self._sizes.clear()
        self._empty_lbl = None
        self._orphan_scan_done = False

        p = self._pal
        games = enumerate_game_caches()

        if not games:
            self._empty_lbl = QLabel(self.tr("No per-game caches found."))
            self._empty_lbl.setStyleSheet(
                f"color:{_c(p,'TEXT_DIM')}; padding:12px;")
            self._rows_v.insertWidget(0, self._empty_lbl)
            return

        for idx, game_dir in enumerate(games):
            name = game_dir.name
            active = (name == self._active)
            label = self.tr("{0}  (active)").format(name) if active else name
            color = _c(p, "TEXT_OK_BRIGHT") if active else _c(p, "TEXT_MAIN")
            self._add_row(idx, name, label, color)

    def _add_row(self, idx: int, key: str, label_text: str, color: str):
        p = self._pal
        row = QWidget()
        row.setStyleSheet(f"background:{_c(p,'BG_PANEL')};")
        h = QHBoxLayout(row)
        h.setContentsMargins(8, 3, 12, 3)
        chk = QCheckBox()
        self._checks[key] = chk
        h.addWidget(chk)
        name_lbl = QLabel(label_text)
        name_lbl.setStyleSheet(f"color:{color}; font-size:13px;")
        h.addWidget(name_lbl, 1)
        size_lbl = QLabel("-")
        size_lbl.setStyleSheet(f"color:{_c(p,'TEXT_DIM')}; font-size:12px;")
        size_lbl.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        size_lbl.setMinimumWidth(80)
        self._size_lbls[key] = size_lbl
        h.addWidget(size_lbl)
        self._rows_v.insertWidget(idx, row)

    # ---- size scan (daemon -> Signal) --------------------------------------
    def _start_size_scan(self):
        """Size the per-game caches, then sweep for orphans - two emits, so the
        (fast) cache sizes land without waiting on the (slower) staging walk."""
        names = [k for k in self._size_lbls if k != _ORPHANS]

        def worker():
            try:
                from Utils.cache_tools import game_cache_sizes
                sizes = dict(game_cache_sizes(names))
            except Exception:
                sizes = {}
            try:
                self._sizes_ready.emit(sizes)
            except (RuntimeError, TypeError):
                return   # widget destroyed mid-scan (signal C++ object gone)
            try:
                from Utils.cache_tools import orphaned_tmp_scan
                dirs, nbytes = orphaned_tmp_scan()
            except Exception:
                dirs, nbytes = [], 0
            try:
                self._orphans_ready.emit(len(dirs), nbytes)
            except (RuntimeError, TypeError):
                pass

        threading.Thread(target=worker, daemon=True).start()

    def _on_sizes(self, sizes: dict):
        from Utils.cache_tools import format_size
        for name, sz in sizes.items():
            self._sizes[name] = sz
            lbl = self._size_lbls.get(name)
            if lbl is not None:
                lbl.setText(format_size(sz))
        self._refresh_total()

    def _on_orphans(self, count: int, nbytes: int):
        """Append the leftover-temp row once the staging sweep finishes."""
        from Utils.cache_tools import format_size
        self._orphan_scan_done = True
        if not count or _ORPHANS in self._checks:
            return
        if self._empty_lbl is not None:
            self._rows_v.removeWidget(self._empty_lbl)
            self._empty_lbl.deleteLater()
            self._empty_lbl = None
        self._add_row(
            max(self._rows_v.count() - 1, 0), _ORPHANS,
            self.tr("Leftover temp folders  ({0})").format(count),
            _c(self._pal, "TEXT_DIM"))
        self._sizes[_ORPHANS] = nbytes
        self._size_lbls[_ORPHANS].setText(format_size(nbytes))
        self._refresh_total()

    def _refresh_total(self):
        from Utils.cache_tools import format_size
        self._total = sum(self._sizes.values())
        self._total_lbl.setText(
            self.tr("Total: {0}").format(format_size(self._total)))

    # ---- selection ---------------------------------------------------------
    def _select_all(self):
        for c in self._checks.values():
            c.setChecked(True)

    def _select_none(self):
        for c in self._checks.values():
            c.setChecked(False)

    def _selected(self) -> list[str]:
        return [k for k, c in self._checks.items() if c.isChecked()]

    # ---- clear actions -----------------------------------------------------
    def _selection_size(self, keys: list[str]) -> int:
        """Sum the already-scanned sizes; only re-walk keys the scan missed
        (it's still running, or it failed) so the confirm prompt is instant."""
        missing = [k for k in keys
                   if k not in self._sizes and k != _ORPHANS]
        if missing:
            from Utils.cache_tools import game_cache_sizes
            self._sizes.update(game_cache_sizes(missing))
        if _ORPHANS in keys and _ORPHANS not in self._sizes:
            from Utils.cache_tools import orphaned_tmp_size
            self._sizes[_ORPHANS] = orphaned_tmp_size()
        return sum(self._sizes.get(k, 0) for k in keys)

    def _label_for(self, key: str) -> str:
        return "Leftover temp folders" if key == _ORPHANS else key

    def _on_clear_selected(self):
        keys = self._selected()
        if not keys:
            self._set_status(self.tr("Nothing selected."), "dim")
            return
        from Utils.cache_tools import format_size
        total = self._selection_size(keys)
        shown = [self._label_for(k) for k in keys]
        listing = "\n".join(f"  • {n}" for n in shown[:10])
        if len(shown) > 10:
            listing += self.tr("\n  • …and {0} more").format(len(shown) - 10)
        body = self.tr("Clear {0} across {1} item(s)?\n\n"
                "{2}\n\nArchives will be re-downloaded as needed.").format(
                    format_size(total), len(keys), listing)
        n = len(keys)
        ConfirmOverlay.show_over(
            self._host,
            self.tr("Clear {0} Cache(s)").format(n), body,
            lambda ok: self._run_clear(keys) if ok else None,
            confirm_label=self.tr("Clear"), cancel_label=self.tr("Cancel"),
            danger=True)

    def _on_clear_all(self):
        # The orphan row lands asynchronously; if the sweep hasn't reported yet
        # finish it here so "Clear All" can't silently skip leftover temp dirs.
        if not self._orphan_scan_done:
            from Utils.cache_tools import orphaned_tmp_scan
            dirs, nbytes = orphaned_tmp_scan()
            self._on_orphans(len(dirs), nbytes)
        keys = list(self._checks.keys())
        if not keys:
            self._set_status(self.tr("Cache is empty."), "dim")
            return
        from Utils.cache_tools import format_size
        total = self._selection_size(keys)
        body = self.tr("Clear {0} of cached downloads across every "
                "game?\n\nLocation: {1}\n\n"
                "The md5 cache is preserved. Archives will be re-downloaded as "
                "needed.").format(format_size(total), get_download_cache_dir())
        ConfirmOverlay.show_over(
            self._host, self.tr("Clear All Download Caches"), body,
            lambda ok: self._run_clear(keys) if ok else None,
            confirm_label=self.tr("Clear"), cancel_label=self.tr("Cancel"),
            danger=True)

    def _run_clear(self, keys: list[str]):
        self._clear_sel_btn.setEnabled(False)
        self._clear_all_btn.setEnabled(False)
        self._set_status(self.tr("Clearing…"), "dim")
        games = [k for k in keys if k != _ORPHANS]
        do_orphans = _ORPHANS in keys

        def worker():
            cleared = 0
            errors: list = []
            try:
                from Utils.cache_tools import (
                    clear_game_caches, clear_orphaned_tmp_dirs)
                c, e = clear_game_caches(games)
                cleared += c
                errors += e
                if do_orphans:
                    c2, e2 = clear_orphaned_tmp_dirs()
                    cleared += c2
                    errors += e2
            except Exception as exc:
                errors.append(str(exc))
            try:
                self._clear_done.emit(cleared, errors)
            except (RuntimeError, TypeError):
                pass

        threading.Thread(target=worker, daemon=True).start()

    def _on_clear_done(self, cleared: int, errors: list):
        self._clear_sel_btn.setEnabled(True)
        self._clear_all_btn.setEnabled(True)
        if errors:
            self._set_status(
                self.tr("Cleared {0}; {1} failed.").format(cleared, len(errors)), "err")
        else:
            self._set_status(
                (self.tr("Cleared 1 cache.") if cleared == 1
                 else self.tr("Cleared {0} caches.").format(cleared)), "ok")
        self._populate()

    def _set_status(self, text: str, kind: str = "dim"):
        color = {
            "ok": _c(self._pal, "TEXT_OK_BRIGHT"),
            "err": _c(self._pal, "TEXT_ERR"),
        }.get(kind, _c(self._pal, "TEXT_DIM"))
        self._status_lbl.setStyleSheet(f"color:{color}; font-size:12px;")
        self._status_lbl.setText(text)

    # ---- close --------------------------------------------------------------
    def _finish(self, result=None):
        """Override: on_closed takes no arguments."""
        if self._done:
            return
        self._done = True
        try:
            self._host.removeEventFilter(self)
        except Exception:
            pass
        cb = self._on_closed
        self.hide()
        self.deleteLater()
        if cb is not None:
            cb()
