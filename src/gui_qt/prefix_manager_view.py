"""Manage Prefixes - browse every isolated tool Wine/Proton prefix and delete
them selectively. Qt port of gui/prefix_manager_overlay.py; discovery / size /
deletion-safety logic is shared via the neutral Utils.prefix_manager.

Opens as a plugins-panel-scoped tab from the Wizard header menu. Enumeration
and per-prefix size calculation run on daemon threads (Signals marshal results
back); deletion is confirmed via ConfirmOverlay, then runs on a worker too.
"""

from __future__ import annotations

import shutil
import threading

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFontMetrics
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QCheckBox,
    QScrollArea, QFrame, QSizePolicy,
)

from gui_qt.theme_qt import active_palette, _c, close_button, button_qss, err_text
from gui_qt.safe_emit import safe_emit
from Utils.prefix_manager import (
    PrefixEntry, enumerate_prefixes, fmt_size, get_dir_size,
    is_deletable_prefix,
)


class _ElidedDetailLabel(QLabel):
    """Detail line that takes whatever width the row offers and elides the
    middle of overlong text (keeps the prefix folder name visible) instead of
    forcing the panel wider or clipping at the right edge. Full text in the
    tooltip."""

    def __init__(self, text=""):
        super().__init__()
        self._full = text
        self.setToolTip(text)
        sp = self.sizePolicy()
        sp.setHorizontalPolicy(QSizePolicy.Ignored)
        self.setSizePolicy(sp)
        self._apply_elide()

    def setText(self, text):  # noqa: N802 (Qt override)
        self._full = text
        self.setToolTip(text)
        self._apply_elide()

    def _apply_elide(self):
        fm = QFontMetrics(self.font())
        super().setText(fm.elidedText(self._full, Qt.ElideMiddle, self.width()))

    def resizeEvent(self, event):  # noqa: N802 (Qt override)
        super().resizeEvent(event)
        self._apply_elide()


class PrefixManagerView(QWidget):
    """Scoped-tab body listing all tool prefixes with sizes + batch delete."""

    _entries_ready = Signal(object)        # list[PrefixEntry]
    _size_ready = Signal(str, "qlonglong")  # (entry key, bytes; 64-bit: >2GB)
    _sizes_done = Signal("qlonglong")      # total bytes (64-bit: >2GB)
    _delete_done = Signal(int, object)     # (deleted count, list[str] errors)

    def __init__(self, active_game_name: str = "", on_close=None, log_fn=None):
        super().__init__()
        self._active_game = active_game_name
        self._on_close = on_close or (lambda: None)
        self._log = log_fn or (lambda _m: None)
        self._entries: list[PrefixEntry] = []
        self._checks: dict[str, QCheckBox] = {}
        self._size_labels: dict[str, QLabel] = {}
        self._sizes: dict[str, int] = {}
        self._busy = False
        self._scan_gen = 0   # invalidates stale size workers after a reload

        self._entries_ready.connect(self._on_entries_ready)
        self._size_ready.connect(self._on_size_ready)
        self._sizes_done.connect(self._on_sizes_done)
        self._delete_done.connect(self._on_delete_done)

        self.setObjectName("PrefixManagerView")
        self._build()
        self._reload()

    # ---- layout -----------------------------------------------------------
    def _build(self):
        p = active_palette()
        self._dim_css = f"color:{_c(p,'TEXT_DIM')};"
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)

        bar = QWidget(); bar.setObjectName("HeaderBar")
        hb = QHBoxLayout(bar); hb.setContentsMargins(12, 8, 8, 8); hb.setSpacing(8)
        title = QLabel(self.tr("Manage Prefixes"))
        title.setStyleSheet(f"color:{_c(p,'TEXT_MAIN')}; font-weight:600;")
        hb.addWidget(title)
        hb.addStretch(1)
        self._total_lbl = QLabel("")
        self._total_lbl.setStyleSheet(self._dim_css)
        hb.addWidget(self._total_lbl)
        close = close_button(pal=p)
        close.clicked.connect(lambda: self._on_close())
        hb.addWidget(close)
        v.addWidget(bar)

        info = QLabel(
            self.tr("Wizard tools each run in their own Wine prefix (created next to "
            "the tool's exe or in the app config folder). Deleting one only "
            "reclaims disk space - it is recreated automatically the next "
            "time the tool runs."))
        info.setWordWrap(True)
        info.setStyleSheet(f"color:{_c(p,'TEXT_DIM')}; padding:8px 12px 4px 12px;")
        v.addWidget(info)

        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.NoFrame)
        self._list_host = QWidget()
        self._list_lay = QVBoxLayout(self._list_host)
        self._list_lay.setContentsMargins(12, 4, 12, 4)
        self._list_lay.setSpacing(4)
        self._list_lay.addStretch(1)
        self._scroll.setWidget(self._list_host)
        v.addWidget(self._scroll, 1)

        self._status = QLabel(self.tr("Scanning for prefixes…"))
        self._status.setStyleSheet(f"color:{_c(p,'TEXT_DIM')}; padding:2px 12px;")
        v.addWidget(self._status)

        row = QWidget()
        rh = QHBoxLayout(row); rh.setContentsMargins(12, 6, 12, 12); rh.setSpacing(8)
        all_btn = QPushButton(self.tr("All"))
        all_btn.clicked.connect(lambda: self._set_all_checked(True))
        rh.addWidget(all_btn)
        none_btn = QPushButton(self.tr("None"))
        none_btn.clicked.connect(lambda: self._set_all_checked(False))
        rh.addWidget(none_btn)
        rh.addStretch(1)
        self._del_sel_btn = QPushButton(self.tr("Delete Selected"))
        self._del_sel_btn.setStyleSheet(
            button_qss("BTN_DANGER", padding="6px 14px"))
        self._del_sel_btn.clicked.connect(self._on_delete_selected)
        rh.addWidget(self._del_sel_btn)
        self._del_all_btn = QPushButton(self.tr("Delete All"))
        self._del_all_btn.setStyleSheet(self._del_sel_btn.styleSheet())
        self._del_all_btn.clicked.connect(self._on_delete_all)
        rh.addWidget(self._del_all_btn)
        v.addWidget(row)

    # ---- data -------------------------------------------------------------
    def _reload(self):
        self._scan_gen += 1
        self._status.setText(self.tr("Scanning for prefixes…"))
        self._total_lbl.setText("")

        def worker():
            try:
                from gui_qt.game_state import _GAMES
                games = dict(_GAMES)
            except Exception:
                games = {}
            try:
                entries = enumerate_prefixes(games)
            except Exception as exc:
                self._log(f"Prefix manager: scan error: {exc}")
                entries = []
            safe_emit(self._entries_ready, entries)

        threading.Thread(target=worker, daemon=True,
                         name="prefix-scan").start()

    def _on_entries_ready(self, entries):
        self._entries = list(entries)
        self._checks.clear()
        self._size_labels.clear()
        self._sizes.clear()

        # Rebuild rows (keep the trailing stretch as the last layout item).
        while self._list_lay.count() > 1:
            item = self._list_lay.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()

        p = active_palette()
        for e in self._entries:
            self._list_lay.insertWidget(self._list_lay.count() - 1,
                                        self._make_row(p, e))

        has_any = bool(self._entries)
        self._del_sel_btn.setEnabled(has_any)
        self._del_all_btn.setEnabled(has_any)
        if not has_any:
            self._status.setText(self.tr("No tool prefixes found."))
            self._total_lbl.setText("")
            return
        self._status.setText(
            (self.tr("1 prefix found - calculating sizes…") if len(self._entries) == 1
             else self.tr("{0} prefixes found - calculating sizes…").format(len(self._entries))))
        self._start_size_scan()

    def _make_row(self, p, e: PrefixEntry) -> QWidget:
        row = QFrame()
        row.setObjectName("PrefixRow")
        row.setStyleSheet(
            f"#PrefixRow {{ background:{_c(p,'BG_PANEL')}; border-radius:6px; }}")
        h = QHBoxLayout(row); h.setContentsMargins(10, 8, 10, 8); h.setSpacing(10)

        chk = QCheckBox()
        self._checks[e.key] = chk
        h.addWidget(chk)

        text = QWidget()
        tv = QVBoxLayout(text); tv.setContentsMargins(0, 0, 0, 0); tv.setSpacing(1)
        active = (self._active_game and e.game == self._active_game)
        name = QLabel(self.tr("{0} - {1}").format(e.tool, e.game)
                      + (self.tr("  (active)") if active else ""))
        name.setStyleSheet(f"color:{_c(p,'TEXT_MAIN')}; font-weight:600;")
        tv.addWidget(name)
        bits = [b for b in (e.location, e.proton, str(e.path.parent)) if b]
        detail = _ElidedDetailLabel("  ·  ".join(bits))
        detail.setStyleSheet(self._dim_css)
        tv.addWidget(detail)
        h.addWidget(text, 1)

        size_lbl = QLabel("…")
        size_lbl.setStyleSheet(self._dim_css)
        size_lbl.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self._size_labels[e.key] = size_lbl
        h.addWidget(size_lbl)
        return row

    def _start_size_scan(self):
        gen = self._scan_gen
        entries = list(self._entries)

        def worker():
            total = 0
            for e in entries:
                if gen != self._scan_gen:
                    return   # a reload superseded this scan
                n = get_dir_size(e.path)
                total += n
                safe_emit(self._size_ready, e.key, n)
            if gen == self._scan_gen:
                safe_emit(self._sizes_done, total)

        threading.Thread(target=worker, daemon=True,
                         name="prefix-sizes").start()

    def _on_size_ready(self, key: str, n: int):
        self._sizes[key] = n
        lbl = self._size_labels.get(key)
        if lbl is not None:
            lbl.setText(fmt_size(n))

    def _on_sizes_done(self, total: int):
        self._total_lbl.setText(self.tr("Total: {0}").format(fmt_size(total)))
        self._status.setText(
            (self.tr("1 prefix found.") if len(self._entries) == 1
             else self.tr("{0} prefixes found.").format(len(self._entries))))

    # ---- selection / delete -------------------------------------------------
    def _set_all_checked(self, on: bool):
        for chk in self._checks.values():
            chk.setChecked(on)

    def _selected_entries(self) -> list[PrefixEntry]:
        return [e for e in self._entries
                if (c := self._checks.get(e.key)) is not None and c.isChecked()]

    def _on_delete_selected(self):
        self._confirm_and_delete(self._selected_entries(), "selected")

    def _on_delete_all(self):
        self._confirm_and_delete(list(self._entries), "all")

    def _confirm_and_delete(self, entries: list[PrefixEntry], what: str):
        if self._busy or not entries:
            return
        n = len(entries)
        size = sum(self._sizes.get(e.key, 0) for e in entries)
        size_txt = f" (~{fmt_size(size)})" if size else ""
        from gui_qt.confirm_overlay import ConfirmOverlay

        def _done(ok: bool, ents=entries):
            if ok:
                self._run_delete(ents)

        ConfirmOverlay.show_over(
            self.window(),
            f"Delete {what} prefixes?",
            f"Delete {n} tool prefix{'' if n == 1 else 'es'}{size_txt}?\n\n"
            "Prefixes are recreated automatically the next time each tool "
            "runs; installed dependencies (e.g. .NET) will re-install then.",
            _done, confirm_label=self.tr("Delete"))

    def _run_delete(self, entries: list[PrefixEntry]):
        self._busy = True
        self._del_sel_btn.setEnabled(False)
        self._del_all_btn.setEnabled(False)
        self._status.setText(self.tr("Deleting…"))

        def worker():
            deleted = 0
            errors: list[str] = []
            for e in entries:
                try:
                    if not is_deletable_prefix(e.path):
                        errors.append(f"skipped non-prefix dir: {e.path}")
                        continue
                    shutil.rmtree(e.path, ignore_errors=True)
                    if e.path.exists():
                        errors.append(f"could not fully delete: {e.path}")
                    else:
                        deleted += 1
                except Exception as exc:
                    errors.append(f"{e.path}: {exc}")
            safe_emit(self._delete_done, deleted, errors)

        threading.Thread(target=worker, daemon=True,
                         name="prefix-delete").start()

    def _on_delete_done(self, deleted: int, errors):
        self._busy = False
        self._log(f"Prefix manager: deleted {deleted} prefix"
                  f"{'' if deleted == 1 else 'es'}.")
        for err in errors or []:
            self._log(f"Prefix manager: {err}")
        if errors:
            self._status.setText(
                self.tr("Deleted {0}; {1} problem(s) - see log.").format(deleted, len(errors)))
            self._status.setStyleSheet(f"color:{err_text()}; padding:2px 12px;")
        self._reload()
