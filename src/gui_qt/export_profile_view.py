"""Export Profile — a modlist-scoped tab that packages the current profile into a
shareable ``.amethyst`` manifest. Qt port of the Tk ``gui/workshop_dialog.py``
(the "Workshop"), renamed to "Export profile".

Per-mod configuration table: Source (Nexus / Direct+URL / Bundle / Ignore), preferred
Version (lazily fetched from Nexus), an Optional flag, and — for mods with FOMOD/BAIN
installer choices — a Fomod-export toggle. Save / Load persist the flags as timestamped
JSON in ``<profile>/workshop/`` (kept for cross-compat with the Tk app); Export writes
the zip. All packaging logic lives in the neutral ``Utils.profile_export``.
"""

from __future__ import annotations

import threading
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import (
    Qt, Signal, QEvent, QT_TRANSLATE_NOOP, QCoreApplication, QTimer,
)
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton,
    QTableWidget, QTableWidgetItem, QCheckBox, QHeaderView, QFrame,
    QRadioButton, QButtonGroup, QListWidget,
    QListWidgetItem, QAbstractItemView,
)

from gui_qt.safe_emit import safe_emit
from gui_qt.theme_qt import active_palette, _c, contrast_text
from Utils import profile_export


_SOURCE_LABELS = {
    "nexus":  QT_TRANSLATE_NOOP("ExportProfileView", "Nexus"),
    "direct": QT_TRANSLATE_NOOP("ExportProfileView", "Direct"),
    "bundle": QT_TRANSLATE_NOOP("ExportProfileView", "Bundle"),
    "ignore": QT_TRANSLATE_NOOP("ExportProfileView", "Ignore"),
}

# Per-source button colours — matched to the Tk workshop (_source_btn_style):
# Nexus orange, Direct green, Bundle purple, Ignore grey. Text is white on all.
_SOURCE_COLORS = {
    "nexus":  ("#c77a3a", "#d98c4c"),   # (base, hover)
    "direct": ("#5a7a5a", "#6b8b6b"),
    "bundle": ("#7a5a7a", "#8b6b8b"),
    "ignore": ("#555555", "#666666"),
}


def _source_button_qss(source: str) -> str:
    base, hover = _SOURCE_COLORS.get(source, _SOURCE_COLORS["nexus"])
    return (f"QPushButton {{ background:{base}; color:{contrast_text(base)}; border:none;"
            f" border-radius:4px; padding:3px 12px; font-weight:600; }}"
            f"QPushButton:hover {{ background:{hover}; }}")


def _card_qss(p) -> str:
    """Full stylesheet for the borderless overlay card and its contents.

    Everything is scoped by object name / type so nothing leaks onto the
    dimmed backdrop (``#CardBackdrop``) or renders with a stray black fill."""
    c = lambda k: _c(p, k)
    return f"""
    #CardBackdrop {{ background: rgba(0,0,0,150); }}
    #OverlayCard {{
        background: {c('BG_HEADER')};
        border: 1px solid {c('BORDER')};
        border-radius: 10px;
    }}
    #OverlayCard QLabel {{ background: transparent; }}
    #CardTitle {{ color: {c('TEXT_MAIN')}; font-weight: 700; font-size: 16px; }}
    #CardSub {{ color: {c('TEXT_DIM')}; font-size: 13px; }}
    /* Radio rows — a subtle pill that lights up on hover / selection. */
    #CardOption {{
        background: {c('BG_DEEP')};
        border: 1px solid transparent;
        border-radius: 6px;
    }}
    #CardOption:hover {{ border: 1px solid {c('BORDER')}; }}
    #CardOption QRadioButton {{
        background: transparent;
        color: {c('TEXT_MAIN')};
        padding: 8px 10px;
        font-size: 13px;
    }}
    #CardOption QRadioButton::indicator {{ width: 15px; height: 15px; }}
    #OverlayCard QLineEdit {{
        background: {c('BG_DEEP')};
        color: {c('TEXT_MAIN')};
        border: 1px solid {c('BORDER')};
        border-radius: 5px;
        padding: 5px 8px;
    }}
    #OverlayCard QLineEdit:focus {{ border: 1px solid {c('ACCENT')}; }}
    #OverlayCard QListWidget {{
        background: {c('BG_DEEP')};
        border: 1px solid {c('BORDER')};
        border-radius: 6px;
    }}
    /* Buttons — Apply (green #GameSelectBtn) inherits the app QSS; give
       Cancel a real neutral style so it isn't a plain black rectangle. */
    #CardCancelBtn {{
        background: {c('BG_ROW')};
        color: {c('TEXT_MAIN')};
        border: 1px solid {c('BORDER')};
        border-radius: 5px;
        padding: 6px 16px;
        font-weight: 600;
    }}
    #CardCancelBtn:hover {{ background: {c('BG_ROW_HOVER')}; }}
    #GameSelectBtn {{
        background: {c('BTN_SUCCESS')};
        color: #ffffff;
        border: none;
        border-radius: 5px;
        padding: 6px 18px;
        font-weight: 600;
    }}
    #GameSelectBtn:hover {{ background: {c('BTN_SUCCESS_HOV')}; }}
    """


# ---------------------------------------------------------------------------
# Borderless overlay base (mirrors nexus_file_chooser.NexusFileChooser) — a
# dimmed, click-absorbing backdrop with a centered card, anchored to the
# top-level window so it covers the whole app (Steam Deck gaming-mode safe:
# a real top-level window can open BEHIND the app).
# ---------------------------------------------------------------------------

class _CardOverlay(QWidget):
    CARD_W = 460
    CARD_H = 300

    def __init__(self, host: QWidget):
        super().__init__(host)
        self._host = host
        self._done = False
        p = active_palette()
        # Scope the dim backdrop to *this* widget by object name — an
        # unqualified ``background`` rule is inherited by every child widget
        # (radios/buttons render black otherwise).
        self.setObjectName("CardBackdrop")
        self.setStyleSheet(_card_qss(p))
        self.setGeometry(host.rect())
        self._card = QFrame(self)
        self._card.setObjectName("OverlayCard")
        self._body = QVBoxLayout(self._card)
        self._body.setContentsMargins(20, 18, 20, 18)
        self._body.setSpacing(10)

    def _show_over(self):
        self._host.installEventFilter(self)
        self._reposition()
        self.show()
        self.raise_()

    def _reposition(self):
        self.setGeometry(self._host.rect())
        w = min(self.CARD_W, self._host.width() - 40)
        h = min(self.CARD_H, self._host.height() - 40)
        self._card.setFixedSize(max(300, w), max(200, h))
        self._card.move((self.width() - self._card.width()) // 2,
                        (self.height() - self._card.height()) // 2)

    def _finish(self):
        if self._done:
            return
        self._done = True
        self._host.removeEventFilter(self)
        self.hide()
        self.deleteLater()

    def mousePressEvent(self, event):
        if not self._card.geometry().contains(event.position().toPoint()):
            self._cancel()

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Escape:
            self._cancel()
        else:
            super().keyPressEvent(event)

    def eventFilter(self, obj, event):
        if obj is self._host and event.type() == QEvent.Resize:
            self._reposition()
        return super().eventFilter(obj, event)

    # Subclasses override.
    def _cancel(self):
        self._finish()


# ---------------------------------------------------------------------------
# Source picker (port of SourcePickerOverlay)
# ---------------------------------------------------------------------------

def _card_title(text: str) -> QLabel:
    lbl = QLabel(text)
    lbl.setObjectName("CardTitle")
    lbl.setWordWrap(True)
    return lbl


def _card_button_bar(overlay, ok_text, on_ok, cancel_text=None):
    if cancel_text is None:
        cancel_text = QCoreApplication.translate("ExportProfileView", "Cancel")
    bar = QHBoxLayout()
    bar.setSpacing(8)
    bar.addStretch(1)
    cancel = QPushButton(cancel_text)
    cancel.setObjectName("CardCancelBtn")
    cancel.setCursor(Qt.PointingHandCursor)
    cancel.clicked.connect(overlay._cancel)
    bar.addWidget(cancel)
    ok = QPushButton(ok_text)
    ok.setObjectName("GameSelectBtn")     # green, matches the file chooser
    ok.setCursor(Qt.PointingHandCursor)
    ok.clicked.connect(on_ok)
    bar.addWidget(ok)
    return bar


class _SourceOverlay(_CardOverlay):
    """Borderless in-window overlay: pick a mod's download source
    (Nexus / Direct+URL / Bundle / Ignore). ``on_pick(source, url)`` on Apply."""

    CARD_W = 480
    CARD_H = 400

    def __init__(self, host, mod_name, current_source, current_url, on_pick):
        super().__init__(host)
        self._on_pick = on_pick
        self._body.addWidget(_card_title(self.tr("Source — {0}").format(mod_name)))

        self._group = QButtonGroup(self)
        self._radios: dict[str, QRadioButton] = {}
        for value, label, desc in (
            ("nexus",  self.tr("Nexus Mods"), self.tr("Download mod from Nexus")),
            ("direct", self.tr("Direct URL"), self.tr("For off-site mods")),
            ("bundle", self.tr("Bundle"),     self.tr("Include mod in the output (e.g. DynDOLOD output)")),
            ("ignore", self.tr("Ignore"),     self.tr("Exclude this mod from the export entirely")),
        ):
            rb = QRadioButton(self.tr("{0}   — {1}").format(label, desc))
            rb.setChecked(value == current_source)
            self._group.addButton(rb)
            self._radios[value] = rb
            rb.toggled.connect(self._on_toggle)
            # Wrap in a styled pill row (#CardOption) for a cleaner look.
            row = QFrame()
            row.setObjectName("CardOption")
            row_l = QVBoxLayout(row)
            row_l.setContentsMargins(0, 0, 0, 0)
            row_l.addWidget(rb)
            self._body.addWidget(row)

        url_row = QHBoxLayout()
        ulbl = QLabel(self.tr("Download URL:"))
        ulbl.setObjectName("CardSub")
        url_row.addWidget(ulbl)
        self._url = QLineEdit(current_url)
        self._url.setPlaceholderText(self.tr("https://…"))
        self._url.setMinimumHeight(30)
        url_row.addWidget(self._url, 1)
        self._url_row_w = QWidget()
        self._url_row_w.setLayout(url_row)
        self._body.addWidget(self._url_row_w)
        self._body.addStretch(1)
        self._body.addLayout(_card_button_bar(self, self.tr("Apply"), self._apply))

        self._on_toggle()
        self._show_over()

    def _current(self) -> str:
        for value, rb in self._radios.items():
            if rb.isChecked():
                return value
        return "nexus"

    def _on_toggle(self):
        self._url_row_w.setVisible(self._current() == "direct")

    def _apply(self):
        src = self._current()
        url = self._url.text().strip() if src == "direct" else ""
        cb = self._on_pick
        self._finish()
        if cb:
            cb(src, url)

    def _cancel(self):
        self._finish()


# ---------------------------------------------------------------------------
# Version picker (port of VersionPickerOverlay)
# ---------------------------------------------------------------------------

class _VersionOverlay(_CardOverlay):
    """Borderless in-window overlay: pick a preferred file version for a Nexus mod.
    Options are ``ver_options`` ({"label", "name", "size_bytes"}); ``on_pick(opt)``
    fires with the chosen option dict."""

    CARD_W = 460
    CARD_H = 380

    def __init__(self, host, mod_name, options, current_label, on_pick):
        super().__init__(host)
        self._on_pick = on_pick
        self._body.addWidget(_card_title(self.tr("Version — {0}").format(mod_name)))
        sub = QLabel(self.tr("Preferred version (file id — version):"))
        sub.setObjectName("CardSub")
        self._body.addWidget(sub)

        self._list = QListWidget()
        self._list.setSelectionMode(QAbstractItemView.SingleSelection)
        for opt in options:
            it = QListWidgetItem(opt.get("label", "—"))
            it.setData(Qt.UserRole, opt)
            self._list.addItem(it)
            if opt.get("label") == current_label:
                self._list.setCurrentItem(it)
        if self._list.currentRow() < 0 and self._list.count():
            self._list.setCurrentRow(0)
        self._list.itemDoubleClicked.connect(lambda _i: self._apply())
        self._body.addWidget(self._list, 1)
        self._body.addLayout(_card_button_bar(self, self.tr("Select"), self._apply))
        self._show_over()

    def _apply(self):
        it = self._list.currentItem()
        result = it.data(Qt.UserRole) if it is not None else None
        cb = self._on_pick
        self._finish()
        if cb and result:
            cb(result)

    def _cancel(self):
        self._finish()


# ---------------------------------------------------------------------------
# Load-settings picker
# ---------------------------------------------------------------------------

class _LoadSettingsOverlay(_CardOverlay):
    """Borderless in-window overlay listing saved settings JSON files (newest
    first). ``on_pick(path)`` fires with the chosen file."""

    CARD_W = 460
    CARD_H = 380

    def __init__(self, host, files, on_pick):
        super().__init__(host)
        self._on_pick = on_pick
        self._files = list(files)
        self._body.addWidget(_card_title(self.tr("Load export settings")))
        sub = QLabel(self.tr("Select a saved settings file:"))
        sub.setObjectName("CardSub")
        self._body.addWidget(sub)
        self._list = QListWidget()
        for f in self._files:
            self._list.addItem(f.name)
        if self._files:
            self._list.setCurrentRow(0)
        self._list.itemDoubleClicked.connect(lambda _i: self._apply())
        self._body.addWidget(self._list, 1)
        self._body.addLayout(_card_button_bar(self, self.tr("Load"), self._apply))
        self._show_over()

    def _apply(self):
        row = self._list.currentRow()
        result = self._files[row] if 0 <= row < len(self._files) else None
        cb = self._on_pick
        self._finish()
        if cb and result:
            cb(result)

    def _cancel(self):
        self._finish()


# ---------------------------------------------------------------------------
# Main view
# ---------------------------------------------------------------------------

# Column indices.
_COL_NAME, _COL_SOURCE, _COL_VERSION, _COL_FOMOD, _COL_OPTIONAL = range(5)


class ExportProfileView(QWidget):
    """Hosted as a modlist-scoped tab (like Profile Settings). Builds export rows
    from the active profile's modlist, lets the user configure per-mod source /
    version / optional / fomod, then writes a ``.amethyst`` manifest."""

    # (data_idx, options) from the version-fetch worker → UI thread.
    _versions_ready = Signal(int, object)
    # (ok, message) from the export worker → UI thread.
    _export_done = Signal(bool, str)
    # (done_bytes, total_bytes, phase) from the export worker → UI thread;
    # total<=0 shows an indeterminate (busy) bar. Byte counts ride as `object` —
    # Signal(int) is C++ int32 and large bundles overflow 2 GiB.
    _export_progress = Signal(object, object, str)
    # pick_save_file's callback fires on the portal WORKER thread; marshal the
    # chosen path to the GUI thread before touching any widget.
    _save_path_picked = Signal(object)
    # (out_path, [(data_idx, size_bytes), …]) from the size-prefetch worker →
    # UI thread, which applies the sizes to _all_rows (the worker must never
    # mutate rows the UI thread reads/sorts) and then starts the packaging.
    _sizes_ready = Signal(str, object)

    def __init__(self, window, game, api, log_fn=None):
        super().__init__()
        self._window = window
        self._game = game
        self._api = api
        self._log = log_fn or (lambda _m: None)
        self._game_domain = getattr(game, "nexus_game_domain", "") or ""

        self._all_rows: list[dict] = []
        self._rows: list[dict] = []   # filtered/sorted view
        self._search_text = ""
        self._hide_no_fileid = False

        self.setObjectName("ExportProfileView")
        self._versions_ready.connect(self._on_versions_ready)
        self._export_done.connect(self._on_export_done)
        self._export_progress.connect(self._on_export_progress)
        self._save_path_picked.connect(self._on_save_path_picked)
        self._sizes_ready.connect(self._on_sizes_ready)
        self._build()
        self._load_rows()
        self._apply_filter()

    # -- construction -------------------------------------------------------
    def _qss(self) -> str:
        p = active_palette()
        c = lambda k: _c(p, k)
        return f"""
        #ExportProfileView {{ background: {c('BG_DEEP')}; }}
        #EPTitleBar {{ background: {c('BG_HEADER')};
                       border-bottom: 1px solid {c('BORDER')}; }}
        #EPTitle {{ color: {c('TEXT_MAIN')}; font-weight: 600; font-size: 15px; }}
        #EPToolbar {{ background: {c('BG_HEADER')}; }}
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

        # Title bar with Close.
        bar = QWidget(); bar.setObjectName("EPTitleBar")
        hb = QHBoxLayout(bar); hb.setContentsMargins(12, 8, 12, 8)
        title = QLabel(self.tr("Export Profile")); title.setObjectName("EPTitle")
        hb.addWidget(title); hb.addStretch(1)
        close = QPushButton(self.tr("✕ Close"))
        close.setObjectName("FormButton")
        close.setCursor(Qt.PointingHandCursor)
        close.clicked.connect(self._close)
        hb.addWidget(close)
        root.addWidget(bar)

        # Toolbar: search + hide-no-fileid + Save / Load / Export.
        tb = QWidget(); tb.setObjectName("EPToolbar")
        tl = QHBoxLayout(tb); tl.setContentsMargins(12, 6, 12, 6); tl.setSpacing(8)
        self._search = QLineEdit()
        self._search.setPlaceholderText(self.tr("Search mods…"))
        self._search.setFixedWidth(220)
        self._search.textChanged.connect(self._on_search)
        tl.addWidget(self._search)
        self._hide_chk = QCheckBox(self.tr("Only mods without a File ID"))
        self._hide_chk.toggled.connect(self._on_hide_toggle)
        tl.addWidget(self._hide_chk)
        tl.addStretch(1)
        save = QPushButton(self.tr("Save settings")); save.setObjectName("FormButton")
        save.setCursor(Qt.PointingHandCursor)
        save.clicked.connect(self._on_save_settings)
        tl.addWidget(save)
        load = QPushButton(self.tr("Load settings")); load.setObjectName("FormButton")
        load.setCursor(Qt.PointingHandCursor)
        load.clicked.connect(self._on_load_settings)
        tl.addWidget(load)
        export = QPushButton(self.tr("Export…")); export.setObjectName("PrimaryButton")
        export.setCursor(Qt.PointingHandCursor)
        export.clicked.connect(self._on_export)
        tl.addWidget(export)
        root.addWidget(tb)

        # Table.
        self._table = QTableWidget(0, 5)
        self._table.setHorizontalHeaderLabels(
            [self.tr("Mod Name"), self.tr("Source"), self.tr("Preferred Version"),
             self.tr("Fomod"), self.tr("Optional")])
        self._table.verticalHeader().setVisible(False)
        self._table.setSelectionMode(QAbstractItemView.NoSelection)
        self._table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        hh = self._table.horizontalHeader()
        hh.setSectionResizeMode(_COL_NAME, QHeaderView.Stretch)
        hh.setSectionResizeMode(_COL_SOURCE, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(_COL_VERSION, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(_COL_FOMOD, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(_COL_OPTIONAL, QHeaderView.ResizeToContents)
        root.addWidget(self._table, 1)

    # -- data ---------------------------------------------------------------
    def _profile_dir(self) -> Path | None:
        pd = getattr(self._game, "_active_profile_dir", None) if self._game else None
        return Path(pd) if pd else None

    def _workshop_dir(self) -> Path | None:
        pd = self._profile_dir()
        return (pd / "workshop") if pd else None

    def _load_rows(self):
        """Build export rows from the active profile's modlist (high-priority first,
        separators dropped — mirrors the Tk _on_workshop prep), then auto-load the
        newest saved settings if present."""
        from Utils.modlist import read_modlist
        pd = self._profile_dir()
        modlist_path = (pd / "modlist.txt") if pd else None
        if not modlist_path or not modlist_path.is_file():
            self._all_rows = []
            return
        entries = [
            e for e in reversed(read_modlist(modlist_path))
            if not e.is_separator
        ]
        self._all_rows = profile_export.load_rows(entries, self._game)
        self._auto_load_latest()

    def _auto_load_latest(self):
        ws_dir = self._workshop_dir()
        if not ws_dir or not ws_dir.is_dir():
            return
        files = sorted(ws_dir.glob("*.json"),
                       key=lambda p: p.stat().st_mtime, reverse=True)
        if files:
            try:
                profile_export.read_settings(files[0], self._all_rows)
                self._log(f"[export] auto-loaded settings: {files[0].name}")
            except Exception:
                pass

    # -- filter / render ----------------------------------------------------
    def _apply_filter(self):
        if self._hide_no_fileid:
            rows = [r for r in self._all_rows if not r.get("file_id")]
        else:
            rows = list(self._all_rows)
        if self._search_text:
            q = self._search_text
            rows = [r for r in rows if q in r["name"].lower()]
        rows.sort(key=lambda r: r["name"].lower())
        self._rows = rows
        self._rebuild_table()

    def _rebuild_table(self):
        t = self._table
        t.setRowCount(0)
        t.setRowCount(len(self._rows))
        # Identity-keyed position map — list.index() would deep-compare dicts
        # per row (O(n²)) and pick the wrong row for structural duplicates.
        pos = {id(r): i for i, r in enumerate(self._all_rows)}
        for i, row in enumerate(self._rows):
            data_idx = pos[id(row)]

            name_item = QTableWidgetItem(row["name"])
            name_item.setFlags(Qt.ItemIsEnabled)
            t.setItem(i, _COL_NAME, name_item)

            src = row.get("source", "nexus")
            src_btn = QPushButton(self.tr(_SOURCE_LABELS.get(src, "Nexus")))
            src_btn.setCursor(Qt.PointingHandCursor)
            src_btn.setStyleSheet(_source_button_qss(src))
            src_btn.clicked.connect(lambda _=False, di=data_idx: self._pick_source(di))
            t.setCellWidget(i, _COL_SOURCE, src_btn)

            ver_btn = QPushButton(row.get("ver_label", "—"))
            ver_btn.setCursor(Qt.PointingHandCursor)
            ver_btn.clicked.connect(lambda _=False, di=data_idx: self._pick_version(di))
            t.setCellWidget(i, _COL_VERSION, ver_btn)

            if row.get("has_fomod"):
                fomod_chk = self._center_checkbox(
                    row.get("fomod_export", True),
                    lambda ch, di=data_idx: self._set_fomod(di, ch))
                t.setCellWidget(i, _COL_FOMOD, fomod_chk)
            else:
                dash = QTableWidgetItem("—")
                dash.setFlags(Qt.ItemIsEnabled)
                dash.setTextAlignment(Qt.AlignCenter)
                t.setItem(i, _COL_FOMOD, dash)

            opt_chk = self._center_checkbox(
                row.get("optional", False),
                lambda ch, di=data_idx: self._set_optional(di, ch))
            t.setCellWidget(i, _COL_OPTIONAL, opt_chk)

    def _center_checkbox(self, checked: bool, on_toggle) -> QWidget:
        wrap = QWidget()
        lay = QHBoxLayout(wrap)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setAlignment(Qt.AlignCenter)
        chk = QCheckBox()
        chk.setChecked(checked)
        chk.toggled.connect(on_toggle)
        lay.addWidget(chk)
        return wrap

    # -- cell actions -------------------------------------------------------
    def _set_optional(self, data_idx: int, checked: bool):
        self._all_rows[data_idx]["optional"] = bool(checked)

    def _set_fomod(self, data_idx: int, checked: bool):
        self._all_rows[data_idx]["fomod_export"] = bool(checked)

    def _pick_source(self, data_idx: int):
        row = self._all_rows[data_idx]

        def _picked(source, url, di=data_idx):
            r = self._all_rows[di]
            r["source"] = source
            r["direct_url"] = url
            self._refresh_visible_row(di)

        _SourceOverlay(self.window(), row["name"], row.get("source", "nexus"),
                       row.get("direct_url", ""), _picked)

    def _pick_version(self, data_idx: int):
        row = self._all_rows[data_idx]
        # Lazily fetch the file list on first open (Nexus mods only).
        if (not row.get("versions_fetched") and row.get("mod_id")
                and self._api is not None and row.get("source", "nexus") == "nexus"):
            row["versions_fetched"] = True
            threading.Thread(
                target=self._fetch_versions, args=(data_idx,),
                daemon=True, name="export-versions").start()
        self._open_version_dialog(data_idx)

    def _open_version_dialog(self, data_idx: int):
        row = self._all_rows[data_idx]
        options = row.get("ver_options") or [
            {"label": row.get("ver_label", "—"), "name": "", "size_bytes": 0}]

        def _picked(sel, di=data_idx):
            r = self._all_rows[di]
            r["ver_label"] = sel.get("label", r["ver_label"])
            r["size_bytes"] = sel.get("size_bytes", 0)
            try:
                r["file_id"] = int(r["ver_label"].split(" — ")[0])
            except (ValueError, IndexError):
                pass
            self._refresh_visible_row(di)

        _VersionOverlay(self.window(), row["name"], options,
                        row.get("ver_label", "—"), _picked)

    def _fetch_versions(self, data_idx: int):
        """Worker thread: fetch the mod's file list from Nexus, marshal back via a
        Signal (never touch widgets off-thread)."""
        row = self._all_rows[data_idx]
        try:
            result = self._api.get_mod_files(self._game_domain, row["mod_id"])
            files = result.files if result else []
        except Exception:
            files = []
        sorted_files = sorted(files, key=lambda f: f.uploaded_timestamp, reverse=True)
        options = [
            {
                "label": f"{f.file_id} — {f.version}",
                "name": f.name,
                "size_bytes": (f.size_in_bytes if f.size_in_bytes is not None
                               else (f.size_kb or 0) * 1024),
            }
            for f in sorted_files if f.file_id
        ]
        safe_emit(self._versions_ready, data_idx, options)

    def _on_versions_ready(self, data_idx: int, options):
        if not options:
            return
        row = self._all_rows[data_idx]
        row["ver_options"] = options
        # Only auto-select if the current label is still a placeholder — opening the
        # picker must not silently change the file_id (port of _apply_versions).
        cur_label = row["ver_label"]
        is_placeholder = (not cur_label or cur_label == "—"
                          or " — " not in cur_label)
        if is_placeholder:
            preferred = str(row["file_id"])
            matched = next(
                (o for o in options if o["label"].startswith(preferred + " —")), None)
            selected = matched or options[0]
            row["ver_label"] = selected["label"]
            row["size_bytes"] = selected["size_bytes"]
            try:
                row["file_id"] = int(selected["label"].split(" — ")[0])
            except (ValueError, IndexError):
                pass
            self._refresh_visible_row(data_idx)

    def _refresh_visible_row(self, data_idx: int):
        """Update the on-screen widgets for one data row if it's currently visible."""
        row = self._all_rows[data_idx]
        try:
            vis_idx = self._rows.index(row)
        except ValueError:
            return
        src_btn = self._table.cellWidget(vis_idx, _COL_SOURCE)
        if isinstance(src_btn, QPushButton):
            src = row.get("source", "nexus")
            src_btn.setText(self.tr(_SOURCE_LABELS.get(src, "Nexus")))
            src_btn.setStyleSheet(_source_button_qss(src))
        ver_btn = self._table.cellWidget(vis_idx, _COL_VERSION)
        if isinstance(ver_btn, QPushButton):
            ver_btn.setText(row.get("ver_label", "—"))

    # -- search / filter toggles --------------------------------------------
    def _on_search(self, text: str):
        self._search_text = (text or "").lower()
        self._apply_filter()

    def _on_hide_toggle(self, checked: bool):
        self._hide_no_fileid = bool(checked)
        self._apply_filter()

    # -- save / load settings -----------------------------------------------
    def _on_save_settings(self):
        if not self._all_rows:
            self._notify(self.tr("Nothing to save."), "warning")
            return
        ws_dir = self._workshop_dir()
        if not ws_dir:
            self._notify(self.tr("No active profile."), "error")
            return
        fname = f"workshop_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        try:
            path = profile_export.write_settings(ws_dir / fname, self._all_rows)
            self._notify(self.tr("Settings saved: {0}").format(path.name), "info")
        except Exception as exc:
            self._notify(self.tr("Save failed: {0}").format(exc), "error")

    def _on_load_settings(self):
        ws_dir = self._workshop_dir()
        if not ws_dir or not ws_dir.is_dir():
            self._notify(self.tr("No saved settings found."), "info")
            return
        files = sorted(ws_dir.glob("*.json"),
                       key=lambda p: p.stat().st_mtime, reverse=True)
        if not files:
            self._notify(self.tr("No saved settings found."), "info")
            return
        def _picked(path):
            try:
                profile_export.read_settings(path, self._all_rows)
                self._apply_filter()
                self._notify(self.tr("Settings loaded."), "info")
            except Exception as exc:
                self._notify(self.tr("Load failed: {0}").format(exc), "error")

        _LoadSettingsOverlay(self.window(), files, _picked)

    # -- export -------------------------------------------------------------
    def _on_export(self):
        if not self._all_rows:
            self._notify(self.tr("No mods to export."), "warning")
            return
        missing = profile_export.nexus_missing_file_ids(self._all_rows)
        if missing:
            count = len(missing)
            noun = self.tr("mod") if count == 1 else self.tr("mods")
            verb = self.tr("is") if count == 1 else self.tr("are")
            self._notify(
                self.tr("{0} Nexus {1} {2} missing a File ID and must be set before exporting.").format(count, noun, verb), "warning")
            return

        pd = self._profile_dir()
        profile_name = pd.name if pd else "manifest"
        default_name = f"{profile_name}_export.amethyst"
        from Utils.portal_filechooser import pick_save_file
        pick_save_file(
            self.tr("Export Amethyst Manifest"),
            lambda path: safe_emit(self._save_path_picked, path),
            current_name=default_name,
            filters=[(self.tr("Amethyst Manifest (*.amethyst)"), ["*.amethyst"]),
                     (self.tr("All files"), ["*"])])

    def _on_save_path_picked(self, path):
        if not path:
            return
        # Immediate feedback (indeterminate) — the worker switches the card to a
        # determinate byte bar once the packaging file list is known.
        self._on_export_progress(0, 0, self.tr("Preparing export…"))
        # Prefetch missing sizes + write on a worker thread.
        threading.Thread(
            target=self._export_worker, args=(str(path),),
            daemon=True, name="export-profile").start()

    def _export_worker(self, out_path: str):
        """Worker thread, phase 1: prefetch missing file sizes into a LOCAL
        list — never mutating _all_rows, which the UI thread reads/sorts — and
        post them back via _sizes_ready; the UI thread applies them and starts
        the packaging worker."""
        # Prefetch file sizes for nexus rows that have mod_id + file_id but no
        # size yet (single batched GraphQL request — port of _prefetch_sizes).
        needs_size = [
            (i, r["mod_id"], r["file_id"])
            for i, r in enumerate(self._all_rows)
            if r.get("mod_id") and r.get("file_id") and not r.get("size_bytes")
            and r.get("source", "nexus") == "nexus"
        ]
        updates: list[tuple[int, int]] = []
        if needs_size and self._api is not None:
            pairs = [(mod_id, file_id) for _i, mod_id, file_id in needs_size]
            try:
                size_map = self._api.graphql_file_sizes_batch(
                    self._game_domain, pairs)
            except Exception:
                size_map = {}
            for i, mod_id, file_id in needs_size:
                sz = size_map.get((mod_id, file_id), 0)
                if sz:
                    updates.append((i, sz))
        safe_emit(self._sizes_ready, out_path, updates)

    def _on_sizes_ready(self, out_path: str, updates):
        """UI thread: apply the prefetched sizes, then package off-thread."""
        for i, sz in updates:
            if 0 <= i < len(self._all_rows):
                self._all_rows[i]["size_bytes"] = sz
        threading.Thread(
            target=self._package_worker, args=(str(out_path),),
            daemon=True, name="export-package").start()

    def _package_worker(self, out_path: str):
        """Worker thread, phase 2: build the manifest and write the archive."""
        try:
            try:
                from version import __version__ as app_version
            except Exception:
                app_version = ""

            game_name = self._game.name if self._game else None
            pd = self._profile_dir()
            manifest = profile_export.build_manifest(
                self._all_rows, self._game_domain, app_version,
                game_name=game_name, profile_dir=pd)

            staging_root = (self._game.get_effective_mod_staging_path()
                            if self._game else None)
            overwrite_root = (self._game.get_effective_overwrite_path()
                              if self._game else None)
            bundle_names = [r["name"] for r in self._all_rows
                            if r.get("source") == "bundle"]

            final = profile_export.write_amethyst(
                out_path, manifest,
                staging_root=staging_root, overwrite_root=overwrite_root,
                profile_dir=pd, bundle_names=bundle_names,
                progress_cb=self._make_progress_cb())
            safe_emit(self._export_done, True, str(final))
        except Exception as exc:
            safe_emit(self._export_done, False, str(exc))

    def _make_progress_cb(self):
        """Throttled bridge from write_amethyst's per-file callback (worker
        thread) to the progress signal — at most ~10 emits/s, but the final
        done==total update always goes through so the bar lands on 100%."""
        import time
        last_emit = [0.0]

        def _cb(done: int, total: int, arcname: str):
            now = time.monotonic()
            if done < total and now - last_emit[0] < 0.1:
                return
            last_emit[0] = now
            parts = arcname.split("/") if arcname else []
            if parts and parts[0] == "mods" and len(parts) > 1:
                phase = self.tr("Packing mod: {0}").format(parts[1])
            elif parts and parts[0] == "overwrite":
                phase = self.tr("Packing overwrite files…")
            elif parts and parts[0] == "profile":
                phase = self.tr("Packing profile files…")
            else:
                phase = self.tr("Packing…")
            safe_emit(self._export_progress, done, total, phase)

        return _cb

    def _progress_popup(self):
        """The window's shared ProgressStack (bottom-right cards), or None."""
        win = self._window
        ensure = getattr(win, "_ensure_feedback", None)
        if callable(ensure):
            try:
                ensure()
            except Exception:
                pass
        return getattr(win, "_progress_popup", None)

    def _on_export_progress(self, done, total, phase: str):
        done, total = int(done), int(total)
        popup = self._progress_popup()
        if popup is None:
            return
        popup.set_progress(done, total, phase,
                           title=self.tr("Exporting profile"),
                           bytes_mode=total > 0, key="profile-export")

    def _on_export_done(self, ok: bool, message: str):
        popup = self._progress_popup()
        if popup is not None:
            if ok:
                # Let the full bar be visible for a beat before dismissing.
                QTimer.singleShot(900, lambda: popup.clear(key="profile-export"))
            else:
                popup.clear(key="profile-export")
        if ok:
            self._notify(self.tr("Exported to {0}").format(message), "info")
            self._log(f"[export] wrote {message}")
        else:
            self._notify(self.tr("Export failed: {0}").format(message), "error")
            self._log(f"[export] failed: {message}")

    # -- misc ---------------------------------------------------------------
    def _close(self):
        tabs = getattr(self._window, "_tabs", None)
        if tabs is not None:
            try:
                tabs.close_tab("export_profile")
                if getattr(self._window, "_export_profile_view", None) is self:
                    self._window._export_profile_view = None
                return
            except Exception:
                pass
        self.hide()

    def _notify(self, text: str, state: str = "info"):
        n = getattr(self._window, "_notify", None)
        if callable(n):
            n(text, state)
        else:
            self._log(text)
