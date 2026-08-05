"""Manual (non-premium) collection install overlay (Qt).

A borderless in-window overlay shown while a manual collection install runs —
the port of the Tk ``_show_manual_install_overlay`` card. Free Nexus users
can't auto-download, so the orchestrator walks the collection one mod at a
time: this card shows the CURRENT mod (name, size, Required/Optional badge,
expected filename) with buttons to open its Nexus download page, batch-open
the next few, pick an already-downloaded file, or skip (optional mods only).
The orchestrator polls the download folders and auto-detects the archive; the
overlay's only channel back to it is ``manual_queue`` (a str path or None).

Like the premium ``CollectionInstallOverlay``, all widgets are built ONCE in
``_build`` and only updated in place (creating widgets later briefly realises
them as top-level windows — see that module's header for the full story), and
every slot here runs on the UI thread. The slot surface is duck-typed to the
premium overlay's so the app's ``_on_col_*`` handlers work on either: the
download/extract slots are no-ops (there is no download pane here).

The one thread-crossing exception is deliberate: the portal file-picker
callback fires on a WORKER thread, so it does nothing but ``manual_queue.put``
(thread-safe, no widgets touched).
"""

from __future__ import annotations

import queue

from PySide6.QtCore import Qt, QEvent
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QFrame,
    QCheckBox,
)

from gui_qt.theme_qt import active_palette, _c

_REQ_TONE = "#3c8a3c"    # "Required" badge (green, matches Tk BG_MOD_REQ)
_OPT_TONE = "#b07c28"    # "Optional" badge (amber, matches Tk BG_MOD_OPT)


def _fmt_size(n: int) -> str:
    n = n or 0
    if n >= 1024 ** 3:
        return f"{n / 1024 ** 3:.1f} GB"
    if n >= 1024 ** 2:
        return f"{n / 1024 ** 2:.1f} MB"
    if n >= 1024:
        return f"{n / 1024:.0f} KB"
    return f"{n} B" if n else ""


class CollectionManualOverlay(QWidget):
    CARD_W = 560

    def __init__(self, host: QWidget, title: str, profile_name: str,
                 mod_count: int, manual_queue: "queue.Queue",
                 on_pause=None, on_cancel=None):
        super().__init__(host)
        self._host = host
        self._queue = manual_queue
        self._on_pause = on_pause
        self._on_cancel = on_cancel
        self._p = active_palette()
        self._total = int(mod_count or 0)
        self._installed_base = 0
        self._installed_fids: set[int] = set()
        self._cur_url = ""
        self._upcoming: list[tuple[str, str]] = []
        self._seen_first = False
        self._finished = False

        self.setObjectName("OverlayBackdrop")
        self.setStyleSheet("#OverlayBackdrop { background: rgba(0,0,0,150); }")
        self.setGeometry(host.rect())
        self._build(title, profile_name)
        host.installEventFilter(self)
        self._reposition()
        self.show()
        self.raise_()

    @classmethod
    def show_over(cls, host, title, profile_name, mod_count, manual_queue,
                  on_pause=None, on_cancel=None):
        top = host.window() if host is not None else None
        return cls(top or host, title, profile_name, mod_count, manual_queue,
                   on_pause=on_pause, on_cancel=on_cancel)

    # ---- build ------------------------------------------------------------
    def _tc(self, k):
        return _c(self._p, k)

    def _build(self, title: str, profile_name: str):
        self._card = QFrame(self)
        self._card.setObjectName("_ManualCard")
        self._card.setStyleSheet(
            f"#_ManualCard {{ background:{self._tc('BG_HEADER')};"
            f" border:1px solid {self._tc('BORDER')}; border-radius:8px; }}")
        outer = QVBoxLayout(self._card)
        outer.setContentsMargins(20, 16, 20, 14)
        outer.setSpacing(6)

        title_lbl = QLabel(self.tr("Manual Download Required"), self._card)
        title_lbl.setAlignment(Qt.AlignHCenter)
        title_lbl.setStyleSheet(
            f"color:{self._tc('TEXT_MAIN')}; font-weight:600; font-size:16px;")
        outer.addWidget(title_lbl)
        sub = QLabel(self.tr("Non-premium users must download each mod manually."),
                     self._card)
        sub.setAlignment(Qt.AlignHCenter)
        sub.setStyleSheet(f"color:{self._tc('TEXT_DIM')}; font-size:11px;")
        outer.addWidget(sub)
        if profile_name:
            prof = QLabel(self.tr("Profile: {0}").format(profile_name), self._card)
            prof.setAlignment(Qt.AlignHCenter)
            prof.setStyleSheet(f"color:{self._tc('TEXT_DIM')}; font-size:12px;")
            outer.addWidget(prof)
        if title:
            col = QLabel(title, self._card)
            col.setAlignment(Qt.AlignHCenter)
            col.setStyleSheet(f"color:{self._tc('TEXT_DIM')}; font-size:12px;")
            outer.addWidget(col)

        # --- mod info card ---
        card = QFrame(self._card)
        card.setObjectName("_ModCard")
        card.setStyleSheet(
            f"#_ModCard {{ background:{self._tc('BG_PANEL')};"
            f" border:1px solid {self._tc('BORDER')}; border-radius:6px; }}")
        cv = QVBoxLayout(card)
        cv.setContentsMargins(12, 10, 12, 10)
        cv.setSpacing(3)
        self._name_lbl = QLabel(self.tr("Preparing…"), card)
        self._name_lbl.setWordWrap(True)
        self._name_lbl.setStyleSheet(
            f"color:{self._tc('TEXT_MAIN')}; font-weight:600; font-size:14px;")
        cv.addWidget(self._name_lbl)
        info_row = QHBoxLayout()
        info_row.setSpacing(8)
        self._size_lbl = QLabel("", card)
        self._size_lbl.setStyleSheet(
            f"color:{self._tc('TEXT_DIM')}; font-size:11px;")
        info_row.addWidget(self._size_lbl)
        self._badge_lbl = QLabel("", card)
        self._badge_lbl.setStyleSheet(
            f"background:{_REQ_TONE}; color:#ffffff; font-weight:600;"
            " font-size:10px; padding:1px 6px; border-radius:3px;")
        self._badge_lbl.hide()
        info_row.addWidget(self._badge_lbl)
        info_row.addStretch(1)
        cv.addLayout(info_row)
        self._hint_lbl = QLabel("", card)
        self._hint_lbl.setWordWrap(True)
        self._hint_lbl.setStyleSheet(
            f"color:{self._tc('TEXT_DIM')}; font-size:10px;"
            " font-family:monospace;")
        cv.addWidget(self._hint_lbl)
        outer.addWidget(card)

        # --- instruction + secondary status ---
        # Two lines: the per-mod instruction (from update_mod) and the shared
        # pipeline status (set_status — "installed N/M…"), so a parallel
        # install finishing doesn't wipe the download instruction.
        self._instr_lbl = QLabel(self.tr("Preparing…"), self._card)
        self._instr_lbl.setWordWrap(True)
        self._instr_lbl.setStyleSheet(
            f"color:{self._tc('TEXT_DIM')}; font-size:12px;")
        outer.addWidget(self._instr_lbl)
        self._status_lbl = QLabel("", self._card)
        self._status_lbl.setWordWrap(True)
        self._status_lbl.setStyleSheet(
            f"color:{self._tc('TEXT_DIM')}; font-size:11px;")
        outer.addWidget(self._status_lbl)

        # --- buttons ---
        btn_row = QHBoxLayout()
        btn_row.setSpacing(8)
        self._open_btn = QPushButton(self.tr("Open Download Page"), self._card)
        self._open_btn.setObjectName("PrimaryButton")
        self._open_btn.setCursor(Qt.PointingHandCursor)
        self._open_btn.clicked.connect(self._open_clicked)
        btn_row.addWidget(self._open_btn)
        self._open_next_btn = QPushButton(self.tr("Open next 5"), self._card)
        self._open_next_btn.setObjectName("FormButton")
        self._open_next_btn.setCursor(Qt.PointingHandCursor)
        self._open_next_btn.clicked.connect(self._open_next_clicked)
        self._open_next_btn.hide()
        btn_row.addWidget(self._open_next_btn)
        self._select_btn = QPushButton(self.tr("Select File…"), self._card)
        self._select_btn.setObjectName("FormButton")
        self._select_btn.setCursor(Qt.PointingHandCursor)
        self._select_btn.clicked.connect(self._select_clicked)
        btn_row.addWidget(self._select_btn)
        self._skip_btn = QPushButton(self.tr("Skip"), self._card)
        self._skip_btn.setObjectName("FormButton")
        self._skip_btn.setCursor(Qt.PointingHandCursor)
        self._skip_btn.clicked.connect(self._skip_clicked)
        self._skip_btn.hide()
        btn_row.addWidget(self._skip_btn)
        btn_row.addStretch(1)
        outer.addLayout(btn_row)

        self._auto_open_chk = QCheckBox(self.tr("Auto open next mod"), self._card)
        outer.addWidget(self._auto_open_chk)

        # --- bottom row: progress + pause/cancel ---
        bottom = QHBoxLayout()
        self._progress_lbl = QLabel(
            self.tr("0 of {0} mods installed").format(self._total), self._card)
        self._progress_lbl.setStyleSheet(
            f"color:{self._tc('TEXT_DIM')}; font-size:11px;")
        bottom.addWidget(self._progress_lbl)
        bottom.addStretch(1)
        self._pause_btn = QPushButton(self.tr("Pause"), self._card)
        self._pause_btn.setObjectName("FormButton")
        self._pause_btn.setCursor(Qt.PointingHandCursor)
        self._pause_btn.clicked.connect(self._pause_clicked)
        # No on_pause → the run can't be resumed (download-only has no profile).
        self._pause_btn.setVisible(self._on_pause is not None)
        bottom.addWidget(self._pause_btn)
        self._cancel_btn = QPushButton(self.tr("Cancel"), self._card)
        self._cancel_btn.setObjectName("DangerButton")
        self._cancel_btn.setCursor(Qt.PointingHandCursor)
        self._cancel_btn.clicked.connect(self._cancel_clicked)
        bottom.addWidget(self._cancel_btn)
        outer.addLayout(bottom)

    # ---- button handlers ----------------------------------------------
    def _open_clicked(self):
        if self._cur_url:
            from Utils.xdg import open_url
            open_url(self._cur_url)

    def _open_next_clicked(self):
        from Utils.xdg import open_url
        if self._cur_url:
            open_url(self._cur_url)
        for _name, url in self._upcoming:
            if url:
                open_url(url)

    def _select_clicked(self):
        from Utils.portal_filechooser import pick_file
        q = self._queue

        def _on_picked(path):
            # WORKER thread — queue.put only, never touch widgets here.
            if path is not None:
                q.put(str(path))

        pick_file("Select downloaded mod archive", _on_picked)

    def _skip_clicked(self):
        self._queue.put(None)

    def _pause_clicked(self):
        self._pause_btn.setEnabled(False)
        self._pause_btn.setText(self.tr("Pausing…"))
        if self._on_pause is not None:
            self._on_pause()

    def _cancel_clicked(self):
        if self._on_cancel is not None:
            self._on_cancel()

    # ---- slots (UI thread only) — duck-typed premium surface ----------
    def update_mod(self, payload: dict):
        """Show the next mod awaiting a manual download (on_manual_mod)."""
        name = payload.get("name") or ""
        self._name_lbl.setText(name)
        self._size_lbl.setText(_fmt_size(payload.get("size", 0)))
        optional = bool(payload.get("optional"))
        self._badge_lbl.setText(self.tr("Optional") if optional else self.tr("Required"))
        self._badge_lbl.setStyleSheet(
            f"background:{_OPT_TONE if optional else _REQ_TONE};"
            " color:#ffffff; font-weight:600; font-size:10px;"
            " padding:1px 6px; border-radius:3px;")
        self._badge_lbl.show()
        self._skip_btn.setVisible(optional)
        fname = payload.get("file_name") or ""
        self._hint_lbl.setText(self.tr("Expected file: {0}").format(fname) if fname else "")
        self._instr_lbl.setText(
            self.tr("Mod {0}/{1} — download this file, then it will be auto-detected…")
            .format(payload.get('idx', 0), payload.get('total', self._total)))
        self._total = int(payload.get("total", self._total) or self._total)
        self._installed_base = int(payload.get("installed_base", 0) or 0)
        self._cur_url = payload.get("url") or ""
        self._upcoming = list(payload.get("upcoming") or [])
        if self._upcoming:
            self._open_next_btn.setText(self.tr("Open next {0}").format(len(self._upcoming) + 1))
            self._open_next_btn.show()
        else:
            self._open_next_btn.hide()
        self._refresh_progress()
        if self._seen_first and self._auto_open_chk.isChecked() and self._cur_url:
            from Utils.xdg import open_url
            open_url(self._cur_url)
        self._seen_first = True
        self._reposition()

    def row_installed(self, file_id: int):
        self._installed_fids.add(int(file_id))
        self._refresh_progress()

    def _refresh_progress(self):
        done = self._installed_base + len(self._installed_fids)
        self._progress_lbl.setText(self.tr("{0} of {1} mods installed").format(done, self._total))

    def set_status(self, text: str):
        self._status_lbl.setText(text or "")

    # Extraction feedback: show the active install on the status line (the
    # closest analogue of Tk's "Installing <name>…" status).
    def extract_add(self, file_id: int, name: str):
        if name:
            self._status_lbl.setText(self.tr("Installing {0}…").format(name))

    # No download pane / aggregate bar in manual mode — accept and ignore the
    # premium-overlay slot calls so the app's handlers need no isinstance.
    def set_display_total(self, n: int):
        pass

    def set_agg(self, cur: int, tot: int, mbps: float):
        pass

    def dl_start(self, file_id: int, name: str, size: int):
        pass

    def dl_update(self, file_id: int, cur: int, tot: int):
        pass

    def dl_finish(self, file_id: int):
        pass

    def extract_queue(self, file_id: int, name: str):
        pass

    def extract_update(self, file_id: int, cur: int, tot: int):
        pass

    def extract_remove(self, file_id: int):
        pass

    # ---- lifecycle ------------------------------------------------------
    def finish(self, message: str = ""):
        self._finished = True
        if message:
            self._instr_lbl.setText(message)
            self._status_lbl.setText("")
        for b in (self._open_btn, self._open_next_btn, self._select_btn,
                  self._skip_btn, self._pause_btn, self._cancel_btn):
            b.setEnabled(False)

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
        self._card.setFixedWidth(max(420, w))
        self._card.adjustSize()
        self._card.move((self.width() - self._card.width()) // 2,
                        (self.height() - self._card.height()) // 2)

    def eventFilter(self, obj, event):
        if obj is self._host and event.type() == QEvent.Resize:
            self._reposition()
        return super().eventFilter(obj, event)
