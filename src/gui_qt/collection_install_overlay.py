"""Collection install progress overlay (Qt).

A borderless in-window overlay (NOT a top-level QDialog - gaming-mode opens
top-levels behind the app), shown while an automatic collection install runs.
Layout mirrors the user's sketch:

  * TOP  - aggregate download bar + "X / Y GB (nn%) - S MB/s".
  * RED  - "Downloading" list: a FIXED POOL of rows (name + thin bar), reused.
  * GREEN- "Installing / Extracting" list: a FIXED POOL of rows (name + thin
           bar, real extraction %) for active installs + one text label for
           overflow-active and queued names.
  * BLUE - Pause + Cancel buttons.

IMPORTANT - why no per-mod widget creation: creating a QWidget/QLabel and only
parenting it afterwards makes it briefly a TOP-LEVEL WINDOW (Qt realises a native
handle on show/polish). Under a collection install that flashed a blank window
per mod AND made kwin balloon to GBs of surface cache (freezing the desktop). So
this overlay builds ALL its widgets ONCE and only updates their text/values in
place - zero widgets are created after ``_build``.

The overlay ONLY renders. The app owns the worker + control Events and connects
its Signals to these slots; every slot runs on the UI thread. Pause/Cancel invoke
the callbacks the app passed in (``on_pause`` / ``on_cancel``).
"""

from __future__ import annotations

from PySide6.QtCore import Qt, QEvent
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QFrame,
    QProgressBar, QSizePolicy, QSpinBox,
)

from gui_qt.theme_qt import active_palette, _c

# Status tones (match the Tk overlay: queued = amber, active = normal text).
_QUEUED_TONE = "#e5a640"
_GREEN_TONE = "#5fb35f"

# Fixed number of visible download rows (matches the Tk overlay's 8 slots).
_DL_SLOTS = 8

# Fixed number of extraction rows with a progress bar (matches the orchestrator's
# max_extract_workers default of 4; extras overflow into the text label).
_EX_SLOTS = 4

# Fixed width of each of the two side panels. Card is CARD_W (720) wide, with
# 16px outer margins each side and 10px spacing between the panels, so each
# panel gets (720 - 32 - 10) / 2 = 339px. Pinned so a long mod name can never
# grow/shrink either panel.
_PANEL_W = 339


def _fmt_gb(n: int) -> str:
    gb = (n or 0) / (1024 ** 3)
    if gb >= 0.1:
        return f"{gb:.1f} GB"
    mb = (n or 0) / (1024 ** 2)
    return f"{mb:.0f} MB"


class _DownloadRow(QWidget):
    """A single active-download slot: mod name + a thin determinate bar. Created
    ONCE (with a parent) into the pool; reused by assigning/clearing a file_id."""

    def __init__(self, parent):
        super().__init__(parent)
        p = active_palette()
        lay = QVBoxLayout(self)
        lay.setContentsMargins(6, 3, 6, 3)
        lay.setSpacing(2)
        self._name = QLabel("", self)
        self._name.setStyleSheet(f"color:{_c(p,'TEXT_MAIN')}; font-size:12px;")
        # Never let a long mod name demand horizontal space (which would grow the
        # fixed-width panel's minimum). Elide long names to a single line instead.
        self._name.setMinimumWidth(0)
        self._name.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        lay.addWidget(self._name)
        self._bar = QProgressBar(self)
        self._bar.setTextVisible(False)
        self._bar.setFixedHeight(6)
        self._bar.setRange(0, 1000)
        self._bar.setValue(0)
        lay.addWidget(self._bar)
        self.hide()

    def assign(self, name: str):
        fm = self._name.fontMetrics()
        self._name.setText(fm.elidedText(name, Qt.ElideRight, _PANEL_W - 28))
        self._bar.setRange(0, 1000)
        self._bar.setValue(0)
        self.show()

    def set_progress(self, cur: int, tot: int):
        if tot > 0:
            self._bar.setRange(0, 1000)
            self._bar.setValue(max(0, min(1000, int(cur * 1000 / tot))))
        else:
            self._bar.setRange(0, 0)

    def clear(self):
        self._name.setText("")
        self._bar.setValue(0)
        self.hide()


class CollectionInstallOverlay(QWidget):
    CARD_W = 720
    CARD_H = 540

    def __init__(self, host: QWidget, title: str, on_pause=None, on_cancel=None,
                 limit_mbps: float = 0.0, on_limit_change=None):
        super().__init__(host)
        self._host = host
        self._on_pause = on_pause
        self._on_cancel = on_cancel
        self._limit_mbps = limit_mbps
        self._on_limit_change = on_limit_change
        self._p = active_palette()
        # file_id → pool-slot index (RED and GREEN; -1 = overflow, no bar row).
        self._dl_slot_of: dict[int, int] = {}
        self._ex_slot_of: dict[int, int] = {}
        self._extract_active: dict[int, str] = {}
        self._extract_queued: dict[int, str] = {}
        self._finished = False
        # True collection size (installed/uncompressed) for the aggregate label;
        # the progress bar itself still tracks compressed download bytes.
        self._display_total = 0

        self.setObjectName("OverlayBackdrop")
        self.setStyleSheet("#OverlayBackdrop { background: rgba(0,0,0,150); }")
        self.setGeometry(host.rect())
        self._build(title)
        host.installEventFilter(self)
        self._reposition()
        self.show()
        self.raise_()

    @classmethod
    def show_over(cls, host, title, on_pause=None, on_cancel=None,
                  limit_mbps: float = 0.0, on_limit_change=None):
        top = host.window() if host is not None else None
        return cls(top or host, title, on_pause=on_pause, on_cancel=on_cancel,
                   limit_mbps=limit_mbps, on_limit_change=on_limit_change)

    # ---- build ------------------------------------------------------------
    def _c(self, k):
        return _c(self._p, k)

    def _panel(self, label: str, accent: str):
        """A titled bordered panel. Returns (frame, content_vbox) so the caller
        adds its (pre-built, parented) content widgets."""
        frame = QFrame(self._card)
        frame.setObjectName("_SecFrame")
        frame.setFixedWidth(_PANEL_W)
        frame.setStyleSheet(
            f"#_SecFrame {{ background:{self._c('BG_PANEL')};"
            f" border:1px solid {self._c('BORDER')}; border-radius:6px; }}")
        v = QVBoxLayout(frame)
        v.setContentsMargins(8, 6, 8, 8)
        v.setSpacing(4)
        hdr = QLabel(label, frame)
        hdr.setStyleSheet(f"color:{accent}; font-weight:600; font-size:12px;")
        v.addWidget(hdr)
        return frame, v

    def _build(self, title: str):
        self._card = QFrame(self)
        self._card.setObjectName("_InstallCard")
        self._card.setStyleSheet(
            f"#_InstallCard {{ background:{self._c('BG_HEADER')};"
            f" border:1px solid {self._c('BORDER')}; border-radius:8px; }}")
        outer = QVBoxLayout(self._card)
        outer.setContentsMargins(16, 14, 16, 14)
        outer.setSpacing(10)

        title_lbl = QLabel(title, self._card)
        title_lbl.setStyleSheet(
            f"color:{self._c('TEXT_MAIN')}; font-weight:600; font-size:15px;")
        outer.addWidget(title_lbl)

        self._agg_lbl = QLabel(self.tr("Preparing…"), self._card)
        self._agg_lbl.setStyleSheet(f"color:{self._c('TEXT_DIM')}; font-size:12px;")
        outer.addWidget(self._agg_lbl)
        self._agg_bar = QProgressBar(self._card)
        self._agg_bar.setTextVisible(False)
        self._agg_bar.setFixedHeight(10)
        self._agg_bar.setRange(0, 1000)
        self._agg_bar.setValue(0)
        outer.addWidget(self._agg_bar)

        self._status_lbl = QLabel("", self._card)
        self._status_lbl.setStyleSheet(f"color:{self._c('TEXT_DIM')}; font-size:12px;")
        outer.addWidget(self._status_lbl)

        lists = QHBoxLayout()
        lists.setSpacing(10)

        # RED - a FIXED pool of download-row widgets (built once, parented now).
        dl_frame, dl_v = self._panel(self.tr("Downloading"), "#c86464")
        self._dl_rows: list[_DownloadRow] = []
        for _ in range(_DL_SLOTS):
            row = _DownloadRow(dl_frame)
            self._dl_rows.append(row)
            dl_v.addWidget(row)
        self._dl_overflow = QLabel("", dl_frame)
        self._dl_overflow.setStyleSheet(f"color:{self._c('TEXT_DIM')}; font-size:11px;")
        dl_v.addWidget(self._dl_overflow)
        dl_v.addStretch(1)

        # GREEN - a FIXED pool of bar rows for active extractions (same widget as
        # the download rows) + ONE text label for overflow-active/queued names.
        ex_frame, ex_v = self._panel(self.tr("Installing / Extracting"), _GREEN_TONE)
        self._ex_rows: list[_DownloadRow] = []
        for _ in range(_EX_SLOTS):
            row = _DownloadRow(ex_frame)
            self._ex_rows.append(row)
            ex_v.addWidget(row)
        self._ex_label = QLabel("", ex_frame)
        self._ex_label.setTextFormat(Qt.RichText)
        self._ex_label.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        self._ex_label.setWordWrap(True)
        self._ex_label.setStyleSheet("font-size:12px;")
        ex_v.addWidget(self._ex_label)
        ex_v.addStretch(1)

        lists.addWidget(dl_frame, 1)
        lists.addWidget(ex_frame, 1)
        outer.addLayout(lists, 1)

        bar = QHBoxLayout()
        limit_lbl = QLabel(self.tr("Speed limit:"), self._card)
        limit_lbl.setStyleSheet(f"color:{self._c('TEXT_DIM')}; font-size:12px;")
        bar.addWidget(limit_lbl)
        self._limit_spin = QSpinBox(self._card)
        self._limit_spin.setRange(0, 250)
        self._limit_spin.setSuffix(self.tr(" MB/s"))
        self._limit_spin.setSpecialValueText(self.tr("Unlimited"))
        self._limit_spin.setValue(int(self._limit_mbps or 0))
        self._limit_spin.setToolTip(self.tr(
            "Cap the combined download speed of this install. "
            "0 = use the full connection. Applies immediately."))
        self._limit_spin.valueChanged.connect(self._limit_changed)
        bar.addWidget(self._limit_spin)
        bar.addStretch(1)
        self._pause_btn = QPushButton(self.tr("Pause"), self._card)
        self._pause_btn.setObjectName("FormButton")
        self._pause_btn.setCursor(Qt.PointingHandCursor)
        self._pause_btn.clicked.connect(self._pause_clicked)
        # No on_pause → the run can't be resumed (download-only has no profile to
        # record a resume point in), so don't offer a button that only stops.
        self._pause_btn.setVisible(self._on_pause is not None)
        bar.addWidget(self._pause_btn)
        self._cancel_btn = QPushButton(self.tr("Cancel"), self._card)
        self._cancel_btn.setObjectName("DangerButton")
        self._cancel_btn.setCursor(Qt.PointingHandCursor)
        self._cancel_btn.clicked.connect(self._cancel_clicked)
        bar.addWidget(self._cancel_btn)
        outer.addLayout(bar)

    # ---- button handlers --------------------------------------------------
    def _limit_changed(self, value: int):
        if self._on_limit_change is not None:
            self._on_limit_change(float(value))

    def _pause_clicked(self):
        self._pause_btn.setEnabled(False)
        self._pause_btn.setText(self.tr("Pausing…"))
        if self._on_pause is not None:
            self._on_pause()

    def _cancel_clicked(self):
        if self._on_cancel is not None:
            self._on_cancel()

    # ---- progress slots (UI thread only) - NO widget creation -------------
    def set_status(self, text: str):
        self._status_lbl.setText(text or "")

    def set_display_total(self, n: int):
        """The true collection size (installed/uncompressed bytes). Shown as the
        '/ Y GB' figure in the aggregate label so it matches the detail header;
        the bar's fill % still comes from compressed download bytes."""
        self._display_total = max(0, int(n or 0))

    def set_agg(self, cur: int, tot: int, mbps: float):
        if tot > 0:
            # Total download bytes can be under- or over-estimated (a mod's
            # size is often unknown → 0, yet its real bytes still accumulate),
            # so clamp the reported fraction to [0, 1] to keep the bar, the
            # percentage and the "X / Y GB" figures sane.
            frac = max(0.0, min(1.0, cur / tot))
            self._agg_bar.setRange(0, 1000)
            self._agg_bar.setValue(int(frac * 1000))
            pct = int(frac * 100)
            # Show the true collection size as the total when known; keep the
            # label internally consistent by scaling the "current" figure to the
            # same download fraction the bar shows (compressed cur/tot).
            shown_tot = self._display_total or tot
            shown_cur = int(frac * shown_tot)
            self._agg_lbl.setText(
                f"{_fmt_gb(shown_cur)} / {_fmt_gb(shown_tot)}  ({pct}%)"
                + (f"  -  {mbps:.1f} MB/s" if mbps > 0 else ""))
        else:
            self._agg_bar.setRange(0, 0)
            self._agg_lbl.setText(self.tr("Downloading…"))

    # RED - assign a pool slot per download; overflow is a count, not widgets.
    def dl_start(self, file_id: int, name: str, size: int):
        if file_id in self._dl_slot_of:
            return
        free = next((i for i in range(_DL_SLOTS) if i not in self._dl_slot_of.values()),
                    None)
        if free is None:
            # All slots busy - track as overflow (count only).
            self._dl_slot_of[file_id] = -1
            self._update_dl_overflow()
            return
        self._dl_slot_of[file_id] = free
        self._dl_rows[free].assign(name)

    def dl_update(self, file_id: int, cur: int, tot: int):
        slot = self._dl_slot_of.get(file_id)
        if slot is not None and slot >= 0:
            self._dl_rows[slot].set_progress(cur, tot)

    def dl_finish(self, file_id: int):
        slot = self._dl_slot_of.pop(file_id, None)
        if slot is not None and slot >= 0:
            self._dl_rows[slot].clear()
            # Promote a waiting overflow download into the freed slot (best-effort:
            # overflow rows have no name/progress, so just clear the counter).
        self._update_dl_overflow()

    def _update_dl_overflow(self):
        extra = sum(1 for v in self._dl_slot_of.values() if v == -1)
        self._dl_overflow.setText(self.tr("+ {0} more downloading…").format(extra) if extra else "")

    # GREEN - assign a pool slot per active extraction; overflow-active and
    # queued names go in the text label (no widget creation).
    def extract_queue(self, file_id: int, name: str):
        self._extract_queued[file_id] = name
        self._render_extract()

    def extract_add(self, file_id: int, name: str):
        self._extract_queued.pop(file_id, None)
        self._extract_active[file_id] = name
        if file_id not in self._ex_slot_of:
            free = next((i for i in range(_EX_SLOTS)
                         if i not in self._ex_slot_of.values()), None)
            self._ex_slot_of[file_id] = -1 if free is None else free
            if free is not None:
                self._ex_rows[free].assign(name)
                # Busy until the first real percent (fallback extractors and the
                # copy/index phases report no numbers).
                self._ex_rows[free].set_progress(0, 0)
        self._render_extract()

    def extract_update(self, file_id: int, cur: int, tot: int):
        slot = self._ex_slot_of.get(file_id)
        if slot is not None and slot >= 0:
            self._ex_rows[slot].set_progress(cur, tot)

    def extract_remove(self, file_id: int):
        self._extract_active.pop(file_id, None)
        self._extract_queued.pop(file_id, None)
        slot = self._ex_slot_of.pop(file_id, None)
        if slot is not None and slot >= 0:
            self._ex_rows[slot].clear()
            # Promote an overflow-active extraction into the freed slot - unlike
            # the download pool we still know its name, so it gains a bar.
            promo = next((f for f, s in self._ex_slot_of.items() if s == -1), None)
            if promo is not None:
                self._ex_slot_of[promo] = slot
                self._ex_rows[slot].assign(self._extract_active.get(promo, ""))
                self._ex_rows[slot].set_progress(0, 0)
        self._render_extract()

    def row_installed(self, file_id: int):
        pass

    def _render_extract(self):
        from html import escape
        lines = []
        for fid, name in self._extract_active.items():
            if self._ex_slot_of.get(fid, -1) == -1:   # no bar row - text line
                lines.append(
                    f"<div style='color:{self._c('TEXT_MAIN')}'>{escape(name)}</div>")
        for name in self._extract_queued.values():
            lines.append(
                f"<div style='color:{_QUEUED_TONE}'>{escape(name)} {self.tr('- Queued')}</div>")
        self._ex_label.setText("".join(lines))

    # ---- lifecycle --------------------------------------------------------
    def finish(self, message: str = ""):
        self._finished = True
        if message:
            self._status_lbl.setText(message)
        self._pause_btn.setEnabled(False)
        self._cancel_btn.setEnabled(False)
        self._limit_spin.setEnabled(False)

    def dismiss(self):
        try:
            self._host.removeEventFilter(self)
        except Exception:
            pass
        self.hide()
        self.deleteLater()

    # ---- geometry ---------------------------------------------------------
    def _reposition(self):
        self.setGeometry(self._host.rect())
        w = min(self.CARD_W, self._host.width() - 40)
        h = min(self.CARD_H, self._host.height() - 40)
        self._card.setFixedSize(max(420, w), max(360, h))
        self._card.move((self.width() - self._card.width()) // 2,
                        (self.height() - self._card.height()) // 2)

    def eventFilter(self, obj, event):
        if obj is self._host and event.type() == QEvent.Resize:
            self._reposition()
        return super().eventFilter(obj, event)
