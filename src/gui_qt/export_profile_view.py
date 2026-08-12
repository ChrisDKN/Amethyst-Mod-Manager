"""Export Profile - a modlist-scoped tab that packages the current profile into a
shareable ``.amethyst`` manifest. Qt port of the Tk ``gui/workshop_dialog.py``
(the "Workshop"), renamed to "Export profile".

Per-mod configuration table: Source (Nexus / Direct+URL / Bundle / Ignore), preferred
Version (lazily fetched from Nexus), an Optional flag, for mods with FOMOD/BAIN
installer choices a Fomod-export toggle, and an Edits toggle that ships local file
changes as binary patches over the pristine download (same mechanism as the
collection creator). Save / Load persist the flags as timestamped JSON in
``<profile>/workshop/`` (kept for cross-compat with the Tk app); Export writes
the zip. All packaging logic lives in the neutral ``Utils.profile_export``.
"""

from __future__ import annotations

import threading
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import (
    Qt, Signal, QEvent, QT_TRANSLATE_NOOP, QCoreApplication, QTimer,
    QIdentityProxyModel,
)
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, QPlainTextEdit,
    QPushButton, QTableWidget, QTableWidgetItem, QCheckBox, QHeaderView,
    QFrame, QRadioButton, QButtonGroup, QListWidget,
    QListWidgetItem, QAbstractItemView,
)

from gui_qt import column_state
from gui_qt.safe_emit import safe_emit
from gui_qt.theme_qt import active_palette, _c, contrast_text
from Utils import profile_export


_SOURCE_LABELS = {
    "nexus":  QT_TRANSLATE_NOOP("ExportProfileView", "Nexus"),
    "thunderstore": QT_TRANSLATE_NOOP("ExportProfileView", "Thunderstore"),
    "direct": QT_TRANSLATE_NOOP("ExportProfileView", "Direct"),
    "browse": QT_TRANSLATE_NOOP("ExportProfileView", "Browse"),
    "manual": QT_TRANSLATE_NOOP("ExportProfileView", "Manual"),
    "bundle": QT_TRANSLATE_NOOP("ExportProfileView", "Bundle"),
    "ignore": QT_TRANSLATE_NOOP("ExportProfileView", "Ignore"),
}

# Per-source button colours - matched to the Tk workshop (_source_btn_style):
# Nexus orange, Direct green, Bundle purple, Ignore grey. Text is white on all.
# Thunderstore blue is deliberately far from Nexus orange: the two stores are
# the pair a user most needs to tell apart at a glance.
_SOURCE_COLORS = {
    "nexus":  ("#c77a3a", "#d98c4c"),   # (base, hover)
    "thunderstore": ("#3a6ea5", "#4a7eb5"),
    "direct": ("#5a7a5a", "#6b8b6b"),
    "browse": ("#5a6b8a", "#6b7c9b"),
    "manual": ("#8a7a4a", "#9b8b5b"),
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
    /* Radio rows - a subtle pill that lights up on hover / selection. */
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
    /* Buttons - Apply (green #GameSelectBtn) inherits the app QSS; give
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
# Borderless overlay base (mirrors nexus_file_chooser.NexusFileChooser) - a
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
        # Scope the dim backdrop to *this* widget by object name - an
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
    (Nexus / Thunderstore / Direct+URL / Browse / Manual / Bundle / Ignore).
    ``on_pick(source, url, instructions)`` on Apply."""

    CARD_W = 480
    # Seven radio rows plus the URL + instructions fields. Sized to fit the
    # Steam Deck's 800px-tall screen without clipping the button bar.
    CARD_H = 570

    def __init__(self, host, mod_name, current_source, current_url, on_pick,
                 current_instructions: str = "", bundle_blocked_reason: str = "",
                 option_notes: dict | None = None,
                 disabled_sources: set | None = None,
                 allowed_sources: "tuple | None" = None):
        """*option_notes* maps a source to a caveat shown as a ⚠ with the text
        on hover; *disabled_sources* greys a source out entirely. Both are how
        the bulk (multi-mod) path reports that a source only fits part of the
        selection - see ExportProfileView._bulk_source_notes.

        *allowed_sources* omits a source from the card completely (not greyed -
        absent). Publishing a Nexus collection uses it to hide Thunderstore,
        which that format cannot express at all; see
        ExportProfileView._ALLOWED_SOURCES."""
        super().__init__(host)
        self._on_pick = on_pick
        option_notes = dict(option_notes or {})
        disabled_sources = set(disabled_sources or ())
        self._body.addWidget(_card_title(self.tr("Source - {0}").format(mod_name)))

        bundle_desc = (bundle_blocked_reason or
                       self.tr("Include mod in the output (e.g. DynDOLOD output)"))
        if bundle_blocked_reason:
            disabled_sources.add("bundle")
        self._group = QButtonGroup(self)
        self._radios: dict[str, QRadioButton] = {}
        for value, label, desc in (
            ("nexus",  self.tr("Nexus Mods"), self.tr("Download mod from Nexus")),
            ("thunderstore", self.tr("Thunderstore"), self.tr("Download package from Thunderstore")),
            ("direct", self.tr("Direct URL"), self.tr("For off-site mods")),
            ("browse", self.tr("Browse page"), self.tr("User downloads from a web page (e.g. Patreon)")),
            ("manual", self.tr("Manual"),     self.tr("User obtains the file; instructions are shown")),
            ("bundle", self.tr("Bundle"),     bundle_desc),
            ("ignore", self.tr("Ignore"),     self.tr("Exclude this mod from the export entirely")),
        ):
            if allowed_sources is not None and value not in allowed_sources:
                continue
            text = self.tr("{0}   - {1}").format(label, desc)
            note = option_notes.get(value, "")
            if note:
                text = f"{text}  ⚠"        # i18n: skip - a bare glyph
            rb = QRadioButton(text)
            if note:
                rb.setToolTip(note)
            rb.setChecked(value == current_source)
            if value in disabled_sources:
                # Redistributing a mod users can download themselves isn't ours
                # to do - see ExportProfileView._bundle_blocked_reason.
                rb.setEnabled(False)
                rb.setChecked(False)
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

        # Shown only while Bundle is selected: bundled files ship inside the
        # collection itself, so the curator is redistributing them.
        self._bundle_note = QLabel(self.tr(
            "Bundled files are distributed with the collection itself. Only "
            "bundle content you have the right to share - generated output "
            "(DynDOLOD, Synthesis), config files, or your own work."))
        self._bundle_note.setObjectName("CardSub")
        self._bundle_note.setWordWrap(True)
        self._body.addWidget(self._bundle_note)

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

        ins_row = QVBoxLayout()
        ilbl = QLabel(self.tr("Instructions shown to the user:"))
        ilbl.setObjectName("CardSub")
        ins_row.addWidget(ilbl)
        self._instructions = QPlainTextEdit(current_instructions)
        self._instructions.setPlaceholderText(
            self.tr("e.g. Download the 2K version from the linked page"))
        self._instructions.setFixedHeight(56)
        ins_row.addWidget(self._instructions)
        self._ins_row_w = QWidget()
        self._ins_row_w.setLayout(ins_row)
        self._body.addWidget(self._ins_row_w)
        self._body.addStretch(1)
        self._body.addLayout(_card_button_bar(self, self.tr("Apply"), self._apply))

        self._on_toggle()
        self._show_over()

    def _current(self) -> str:
        """The picked source, or "" when none is. Empty is reachable two ways:
        a bulk change over rows that don't share one source, and a row whose
        saved source is now disabled. Both must mean "nothing chosen yet"
        rather than silently falling through to Nexus."""
        for value, rb in self._radios.items():
            if rb.isChecked():
                return value
        return ""

    def _on_toggle(self):
        src = self._current()
        self._url_row_w.setVisible(src in ("direct", "browse", "manual"))
        self._ins_row_w.setVisible(src in ("browse", "manual"))
        self._bundle_note.setVisible(src == "bundle")

    def _apply(self):
        src = self._current()
        if not src:
            self._finish()      # nothing picked - close without changing rows
            return
        url = self._url.text().strip() if src in ("direct", "browse", "manual") else ""
        instructions = (self._instructions.toPlainText().strip()
                        if src in ("browse", "manual") else "")
        cb = self._on_pick
        self._finish()
        if cb:
            cb(src, url, instructions)

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

    def __init__(self, host, mod_name, options, current_label, on_pick,
                 loading: bool = False):
        super().__init__(host)
        self._on_pick = on_pick
        self._body.addWidget(_card_title(self.tr("Version - {0}").format(mod_name)))
        sub = QLabel(self.tr("Preferred version (file id - version):"))
        sub.setObjectName("CardSub")
        self._body.addWidget(sub)

        self._list = QListWidget()
        self._list.setSelectionMode(QAbstractItemView.SingleSelection)
        self._list.itemDoubleClicked.connect(lambda _i: self._apply())
        self._body.addWidget(self._list, 1)
        # The Nexus file list is fetched lazily, so the card can open before
        # the versions arrive; set_options() fills them in when they land.
        self._status = QLabel(self.tr("Fetching versions from Nexus…"))
        self._status.setObjectName("CardSub")
        self._status.setVisible(loading)
        self._body.addWidget(self._status)
        self._body.addLayout(_card_button_bar(self, self.tr("Select"), self._apply))
        self.set_options(options, current_label)
        self._show_over()

    def set_options(self, options, current_label=None):
        """Repopulate the list, keeping the current selection where possible."""
        if current_label is None:
            it = self._list.currentItem()
            current_label = it.text() if it is not None else None
        self._list.clear()
        for opt in options or []:
            item = QListWidgetItem(opt.get("label", "-"))
            item.setData(Qt.UserRole, opt)
            self._list.addItem(item)
            if opt.get("label") == current_label:
                self._list.setCurrentItem(item)
        if self._list.currentRow() < 0 and self._list.count():
            self._list.setCurrentRow(0)

    def set_loading(self, loading: bool, message: str = ""):
        if message:
            self._status.setText(message)
        self._status.setVisible(bool(loading) or bool(message))

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
(_COL_CHECK, _COL_NAME, _COL_SOURCE, _COL_VERSION, _COL_FOMOD, _COL_OPTIONAL,
 _COL_EDITS) = range(7)

# The bulk-select column's header is blank - the checkboxes speak for
# themselves and a label would only widen the column. It is still the
# column_state key its width is persisted under, so it must stay stable.
_CHECK_COL_NAME = ""


class _HeaderModelShim(QIdentityProxyModel):
    """Adds TkStyleHeader's text/deco suppression contract on top of the
    QTableWidget's internal model. The header paints labels itself (elided
    clear of its sort triangle) and expects headerData to honour
    ``_suppress_header_text`` during the chrome pass - without this shim the
    native label is painted too and every column shows its text twice."""

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if orientation == Qt.Horizontal:
            if role == Qt.DisplayRole and getattr(
                    self, "_suppress_header_text", False):
                return ""
            if role == Qt.DecorationRole and getattr(
                    self, "_suppress_header_deco", False):
                return None
        return super().headerData(section, orientation, role)


class ExportProfileView(QWidget):
    """Hosted as a modlist-scoped tab (like Profile Settings). Builds export rows
    from the active profile's modlist, lets the user configure per-mod source /
    version / optional / fomod, then writes a ``.amethyst`` manifest."""

    # (data_idx, options) from the version-fetch worker → UI thread.
    _versions_ready = Signal(int, object)
    # (ok, message) from the export worker → UI thread.
    _export_done = Signal(bool, str)
    # (done_bytes, total_bytes, phase) from the export worker → UI thread;
    # total<=0 shows an indeterminate (busy) bar. Byte counts ride as `object` -
    # Signal(int) is C++ int32 and large bundles overflow 2 GiB.
    _export_progress = Signal(object, object, str)
    # pick_save_file's callback fires on the portal WORKER thread; marshal the
    # chosen path to the GUI thread before touching any widget.
    _save_path_picked = Signal(object)
    # (out_path, [(data_idx, size_bytes), …]) from the size-prefetch worker →
    # UI thread, which applies the sizes to _all_rows (the worker must never
    # mutate rows the UI thread reads/sorts) and then starts the packaging.
    _sizes_ready = Signal(str, object)
    # (report, proceed) from the archive-preflight worker → UI thread.
    _archive_check_done = Signal(object, object)

    # Default disabled mods to "ignore"? Off here: the ``.amethyst`` manifest
    # carries ``enabled: false`` through to the importer, so a disabled mod is
    # exportable. Nexus collections have no disabled state and drop those rows,
    # so CreateCollectionView turns this on.
    _IGNORE_DISABLED_ROWS = False
    # Does the export need the archive for FOMOD choices? Off here: `.amethyst`
    # embeds the raw selection sidecar, no installer config required. Collection
    # exports map choices through fomod/ModuleConfig.xml, so the subclass turns
    # this on.
    _ARCHIVE_PREFLIGHT_FOMOD = False
    # Sources this view may export. The ``.amethyst`` manifest can express all
    # of them; the Nexus-collection format has no way to represent a
    # Thunderstore package, so CreateCollectionView drops it from the list AND
    # resets any auto-detected row (see its _seed_rows).
    _ALLOWED_SOURCES = ("nexus", "thunderstore", "direct", "browse", "manual",
                        "bundle", "ignore")

    def __init__(self, window, game, api, log_fn=None):
        super().__init__()
        self._window = window
        self._game = game
        self._api = api
        self._log = log_fn or (lambda _m: None)
        self._game_domain = getattr(game, "nexus_game_domain", "") or ""

        self._all_rows: list[dict] = []
        self._rows: list[dict] = []   # filtered/sorted view
        # Widget reuse across rebuilds - see _rebuild_table. _row_items[i] /
        # _row_pool[i] hold visible row i's QTableWidgetItems / cell widgets;
        # _vis_data_idx[i] is the _all_rows index shown there, resolved by the
        # pooled widgets' handlers at click time.
        self._row_items: list[dict] = []
        self._row_pool: list[dict] = []
        self._vis_data_idx: list[int] = []
        self._fill_timer = QTimer(self)
        self._fill_timer.setSingleShot(True)
        self._fill_timer.timeout.connect(self._fill_more_rows)
        self._search_text = ""
        self._hide_no_fileid = False
        self._sort_col = _COL_NAME
        self._sort_asc = True
        self._restoring_columns = False
        self._vp_filter_installed = False
        # Bulk selection: data indices into _all_rows (stable - _all_rows is
        # only ever replaced wholesale by _load_rows, never re-ordered).
        # _check_anchor + _check_range_select drive shift-click ranges the same
        # way DownloadsModel.toggle_check does.
        self._checked: set[int] = set()
        self._check_anchor: int | None = None
        self._check_range_select = True
        # (data_idx, overlay) for the version card currently on screen, so a
        # late file-list fetch can populate it instead of arriving too late.
        self._version_overlay = None

        self.setObjectName("ExportProfileView")
        self._versions_ready.connect(self._on_versions_ready)
        self._export_done.connect(self._on_export_done)
        self._export_progress.connect(self._on_export_progress)
        self._save_path_picked.connect(self._on_save_path_picked)
        self._sizes_ready.connect(self._on_sizes_ready)
        self._archive_check_done.connect(self._on_archive_check_done)
        self._build()
        self._load_rows()
        self._apply_filter()

    # -- construction -------------------------------------------------------
    def _qss(self) -> str:
        from gui_qt.theme_qt import _tinted_icon_url
        p = active_palette()
        c = lambda k: _c(p, k)
        ct = lambda k: contrast_text(_c(p, k))
        # The select column's tick is an item-view indicator, not a QCheckBox,
        # so the app theme's QCheckBox::indicator rules don't reach it. Repeat
        # them here or it renders as a bare platform checkbox next to the
        # themed Optional/Fomod boxes three columns over.
        check_qss = f"""
        QTableWidget::indicator {{
            width: 16px; height: 16px;
            border: 1px solid {c('BORDER_FAINT')};
            border-radius: 3px;
            background: {c('BG_DEEP')};
        }}
        QTableWidget::indicator:hover {{ border: 1px solid {c('CHECK_FILL')}; }}
        QTableWidget::indicator:checked {{
            background: {c('CHECK_FILL')};
            border: 1px solid {c('CHECK_FILL')};
            image: url({_tinted_icon_url('check_white.png', ct('CHECK_FILL'))});
        }}
        """
        return check_qss + f"""
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
        # Bulk actions on the ticked rows - disabled until something is ticked.
        self._bulk_source_btn = QPushButton(self.tr("Change Source"))
        self._bulk_source_btn.setObjectName("FormButton")
        self._bulk_source_btn.setCursor(Qt.PointingHandCursor)
        self._bulk_source_btn.clicked.connect(self._on_bulk_source)
        tl.addWidget(self._bulk_source_btn)
        self._bulk_optional_btn = QPushButton(self.tr("Make Optional"))
        self._bulk_optional_btn.setObjectName("FormButton")
        self._bulk_optional_btn.setCursor(Qt.PointingHandCursor)
        self._bulk_optional_btn.clicked.connect(self._on_bulk_optional)
        tl.addWidget(self._bulk_optional_btn)
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
        self._table = QTableWidget(0, 7)
        self._table.setHorizontalHeaderLabels(
            [_CHECK_COL_NAME,
             self.tr("Mod Name"), self.tr("Source"), self.tr("Preferred Version"),
             self.tr("Fomod"), self.tr("Optional"), self.tr("Edits")])
        self._table.verticalHeader().setVisible(False)
        self._table.setSelectionMode(QAbstractItemView.NoSelection)
        self._table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self._table.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        root.addWidget(self._table, 1)
        self._init_header()

    # -- header: click-to-sort + Tk-style resize, persisted per view ---------
    # Sorting is DISPLAY-ONLY: exports always read _all_rows, whose order is
    # the profile's mod priority. Re-sorting the view never changes what the
    # manifest records.
    # Resizing mirrors the modlist panel: TkStyleHeader conserves the total
    # width on boundary drags (grow one side by exactly what the other frees),
    # and _fit_name_to_width keeps Mod Name absorbing viewport changes - so
    # the columns always reach the panel edge and can't be dragged past it.
    _COLUMN_STATE_SECTION = "qt_columns_export_profile"
    _DEFAULT_COL_WIDTHS = {"Mod Name": 320, "Preferred Version": 150}
    _NAME_MIN = 160
    _COL_MIN_DEFAULT = 60
    # Content-driven floors: wide enough for the cell widgets (source button,
    # version label, centered checkbox) so shrinking the panel never clips
    # them. The header-label fit below raises these further when needed.
    _MIN_COL_WIDTHS = {
        _CHECK_COL_NAME: 34,   # just the tick indicator
        "Mod Name": _NAME_MIN,
        "Source": 84,
        "Preferred Version": 130,
        "Fomod": 70,
        "Optional": 78,
        "Edits": 58,
    }
    # Where the Edits (save_edits) column sits - CreateCollectionView inserts
    # Phase/Update before it and overrides this.
    _EDITS_COL = _COL_EDITS

    def _column_names(self) -> list:
        t = self._table
        return [(t.horizontalHeaderItem(c).text()
                 if t.horizontalHeaderItem(c) is not None else str(c))
                for c in range(t.columnCount())]

    def _col_mins(self, names: list) -> dict:
        # Floor each column at its content minimum or its full header label
        # (plus the sort-triangle strip), whichever is larger - column text
        # only elides below the user's chosen width, never at the minimum.
        fm = self._table.fontMetrics()
        label_pad = 12 + 4 + 2 + 10 + 4   # _TRI_W + _TRI_PAD + gap + margins
        return {
            c: max(self._MIN_COL_WIDTHS.get(name, self._COL_MIN_DEFAULT),
                   fm.horizontalAdvance(name) + label_pad)
            for c, name in enumerate(names)
        }

    def _init_header(self):
        """(Re)install the Tk-style header after the column set is final."""
        from gui_qt.modlist_header import TkStyleHeader

        names = self._column_names()
        col_mins = self._col_mins(names)
        # A fresh header instance each call (the subclass re-runs this after
        # adding columns), so connections below never double-fire.
        hdr = TkStyleHeader(self._table, col_mins)
        self._table.setHorizontalHeader(hdr)
        # The header needs a model honouring the text-suppression contract -
        # QTableWidget's internal model doesn't, so labels would paint twice.
        shim = _HeaderModelShim(hdr)
        shim.setSourceModel(self._table.model())
        hdr.setModel(shim)
        hdr.setMinimumSectionSize(min(col_mins.values()))
        hdr.setSectionsMovable(False)
        hdr.setSectionsClickable(True)
        # TkStyleHeader paints a triangle on every sortable column itself.
        hdr.setSortIndicatorShown(False)
        self._table.sort_triangle_spec = (
            lambda lg: (lg == self._sort_col, self._sort_asc))
        # Boundary-drag release persists widths via this view hook.
        self._table._schedule_save = self._save_column_state
        hdr.sectionClicked.connect(self._on_header_clicked)
        hdr.sectionResized.connect(self._on_section_resized)
        hdr.show()

        for col, name in enumerate(names):
            width = self._DEFAULT_COL_WIDTHS.get(name, 0)
            self._table.setColumnWidth(
                col, max(width, self._table.columnWidth(col),
                         col_mins.get(col, self._COL_MIN_DEFAULT)))

        # Restore persisted widths + sort.
        try:
            state = column_state.load_state(self._COLUMN_STATE_SECTION,
                                            columns=names)
        except Exception:
            state = {"widths": {}, "sort_col": None, "ascending": True}
        self._restoring_columns = True
        try:
            for col, name in enumerate(names):
                width = state["widths"].get(name)
                if width and width > 20:
                    # Clamp persisted widths up to the (possibly raised) floor.
                    self._table.setColumnWidth(
                        col, max(int(width), col_mins.get(col, 0)))
        finally:
            self._restoring_columns = False
        if state.get("sort_col") in names:
            self._sort_col = names.index(state["sort_col"])
            self._sort_asc = bool(state.get("ascending", True))
        hdr.setSortIndicator(
            self._sort_col, Qt.AscendingOrder if self._sort_asc
            else Qt.DescendingOrder)

        if not getattr(self, "_vp_filter_installed", False):
            self._table.viewport().installEventFilter(self)
            self._vp_filter_installed = True
        self._fit_name_to_width()

    def eventFilter(self, obj, event):
        if obj is self._table.viewport():
            if event.type() == QEvent.Resize:
                self._fit_name_to_width()
            elif (event.type() in (QEvent.MouseButtonRelease,
                                   QEvent.MouseButtonDblClick)
                    and event.button() == Qt.LeftButton):
                # Downloads-tab parity: the tick OR the mod name toggles the
                # row, so a shift-range is easy to drag out. Double-clicks are
                # handled too, else a fast second click is swallowed and the
                # row appears not to respond.
                idx = self._table.indexAt(event.position().toPoint())
                if idx.isValid() and idx.column() in (_COL_CHECK, _COL_NAME):
                    self._toggle_check(
                        idx.row(),
                        shift=bool(event.modifiers() & Qt.ShiftModifier))
                    return True
        return super().eventFilter(obj, event)

    # -- bulk selection ------------------------------------------------------
    def _data_index_map(self) -> dict:
        """``id(row) -> index into _all_rows``. Identity-keyed: list.index()
        would deep-compare dicts and pick the wrong row for duplicates."""
        return {id(r): i for i, r in enumerate(self._all_rows)}

    def _toggle_check(self, vis_row: int, shift: bool = False):
        """Toggle the tick on a VISIBLE row. With *shift* and a live anchor,
        apply the anchor's last action to the whole visible range between them
        (port of DownloadsModel.toggle_check). Ranges walk the rows as sorted
        and filtered on screen, which is what the user is pointing at."""
        if not (0 <= vis_row < len(self._rows)):
            return
        pos = self._data_index_map()
        data_idx = pos.get(id(self._rows[vis_row]))
        if data_idx is None:
            return
        if shift and self._check_anchor is not None:
            anchor_vis = next(
                (v for v, r in enumerate(self._rows)
                 if pos.get(id(r)) == self._check_anchor), None)
            if anchor_vis is not None:
                lo, hi = sorted((anchor_vis, vis_row))
                for v in range(lo, hi + 1):
                    di = pos.get(id(self._rows[v]))
                    if di is None:
                        continue
                    if self._check_range_select:
                        self._checked.add(di)
                    else:
                        self._checked.discard(di)
                # Anchor stays put so the range can be re-extended.
                self._refresh_check_column()
                self._update_bulk_actions()
                return
        if data_idx in self._checked:
            self._checked.discard(data_idx)
            self._check_range_select = False
        else:
            self._checked.add(data_idx)
            self._check_range_select = True
        self._check_anchor = data_idx
        self._refresh_check_column()
        self._update_bulk_actions()

    def _refresh_check_column(self):
        """Re-stamp every visible tick from ``_checked``."""
        pos = self._data_index_map()
        for v, row in enumerate(self._rows):
            item = self._table.item(v, _COL_CHECK)
            if item is None:
                continue
            di = pos.get(id(row))
            item.setCheckState(Qt.Checked if di in self._checked
                               else Qt.Unchecked)

    def _checked_rows(self) -> list:
        """The ticked rows, in ``_all_rows`` order. Ticks survive search and
        filter changes (Downloads parity), so this can include rows that are
        currently hidden - the button labels carry the count to make that
        visible before the user acts."""
        return [self._all_rows[i] for i in sorted(self._checked)
                if 0 <= i < len(self._all_rows)]

    def _clear_checks(self):
        self._checked.clear()
        self._check_anchor = None
        self._check_range_select = True

    def _update_bulk_actions(self):
        """Enable/label the bulk buttons for the current tick set."""
        rows = self._checked_rows()
        n = len(rows)
        for btn in (self._bulk_source_btn, self._bulk_optional_btn):
            btn.setEnabled(bool(n))
        self._bulk_source_btn.setText(
            self.tr("Change Source ({0})").format(n) if n
            else self.tr("Change Source"))
        # One button, both directions: an all-optional selection clears, any
        # other selection sets. The label always states what a click will do.
        all_optional = bool(rows) and all(r.get("optional") for r in rows)
        if not n:
            self._bulk_optional_btn.setText(self.tr("Make Optional"))
        elif all_optional:
            self._bulk_optional_btn.setText(
                self.tr("Clear Optional ({0})").format(n))
        else:
            self._bulk_optional_btn.setText(
                self.tr("Make Optional ({0})").format(n))

    def _bulk_source_notes(self, rows) -> "tuple[dict, set]":
        """``(per-source warning text, sources to disable)`` for a bulk change.

        A selection is usually mixed, so instead of hiding sources that only
        some rows can use, each one carries what it will do to the rows that
        can't - matching the single-mod overlay, which explains rather than
        silently drops."""
        total = len(rows)
        notes: dict = {}
        disabled: set = set()

        blocked = [r for r in rows if self._bundle_blocked_reason(r)]
        if blocked:
            if len(blocked) == total:
                disabled.add("bundle")
                notes["bundle"] = self._bundle_blocked_reason(blocked[0])
            else:
                notes["bundle"] = self.tr(
                    "{0} of {1} selected mods are on Nexus and will be left "
                    "unchanged.").format(len(blocked), total)

        no_file = [r for r in rows if not (r.get("mod_id") and r.get("file_id"))]
        if no_file:
            notes["nexus"] = self.tr(
                "{0} of {1} selected mods have no Nexus File ID yet - set one "
                "on each before exporting.").format(len(no_file), total)

        if "thunderstore" in self._ALLOWED_SOURCES:
            no_ts = [r for r in rows if self._thunderstore_blocked_reason(r)]
            if no_ts:
                if len(no_ts) == total:
                    disabled.add("thunderstore")
                    notes["thunderstore"] = self.tr(
                        "None of the selected mods were installed from "
                        "Thunderstore.")
                else:
                    notes["thunderstore"] = self.tr(
                        "{0} of {1} selected mods were not installed from "
                        "Thunderstore and will be left unchanged.").format(
                            len(no_ts), total)

        if total > 1:
            shared = self.tr(
                "The same URL and instructions are applied to all {0} selected "
                "mods.").format(total)
            for key in ("direct", "browse", "manual"):
                notes[key] = shared
        return notes, disabled

    def _on_bulk_source(self):
        rows = self._checked_rows()
        if not rows:
            return
        notes, disabled = self._bulk_source_notes(rows)

        def _unanimous(key: str, default: str = "") -> str:
            """The value all rows share, else "" - a mixed selection starts the
            field blank rather than silently adopting one row's value."""
            vals = {r.get(key, default) or "" for r in rows}
            return vals.pop() if len(vals) == 1 else ""

        current = _unanimous("source", "nexus")
        count = len(rows)
        label = (rows[0]["name"] if count == 1
                 else self.tr("{0} mods").format(count))

        def _picked(source, url, instructions):
            self._apply_bulk_source(source, url, instructions)

        _SourceOverlay(self.window(), label, current, _unanimous("direct_url"),
                       _picked,
                       current_instructions=_unanimous("source_instructions"),
                       option_notes=notes, disabled_sources=disabled,
                       allowed_sources=self._ALLOWED_SOURCES)

    def _apply_bulk_source(self, source: str, url: str, instructions: str):
        changed = skipped = 0
        ts_skipped = 0
        for row in self._checked_rows():
            # The bundle rule is per-mod, so a bulk change must not become a
            # way around it - those rows keep the source they had.
            if source == "bundle" and self._bundle_blocked_reason(row):
                skipped += 1
                continue
            # Same for Thunderstore: a mod with no pin has nothing to download,
            # so a bulk set would produce an entry the importer cannot resolve.
            if source == "thunderstore" and self._thunderstore_blocked_reason(row):
                ts_skipped += 1
                continue
            row["source"] = source
            row["direct_url"] = url
            row["source_instructions"] = instructions
            changed += 1
        # Full rebuild: source drives the Edits cell and can drive the sort.
        self._apply_filter()
        if ts_skipped:
            self._notify(
                self.tr("Source set on {0} mod(s); {1} left unchanged (not "
                        "from Thunderstore).").format(changed, ts_skipped),
                "warning")
        elif skipped:
            self._notify(
                self.tr("Source set on {0} mod(s); {1} left unchanged (on "
                        "Nexus).").format(changed, skipped), "warning")
        else:
            self._notify(
                self.tr("Source set on {0} mod(s).").format(changed), "info")

    def _on_bulk_optional(self):
        rows = self._checked_rows()
        if not rows:
            return
        make = not all(r.get("optional") for r in rows)
        for row in rows:
            row["optional"] = make
        self._apply_filter()
        self._notify(
            self.tr("Marked {0} mod(s) optional.").format(len(rows)) if make
            else self.tr("Cleared optional on {0} mod(s).").format(len(rows)),
            "info")

    def _fit_name_to_width(self):
        """Keep the columns exactly filling the viewport (modlist behaviour).

        Growing: Mod Name absorbs the extra. Shrinking: Mod Name gives back
        down to its minimum, then the other columns cascade toward theirs."""
        vp = self._table.viewport().width()
        if vp <= 0:
            return
        count = self._table.columnCount()
        hdr = self._table.horizontalHeader()
        others = sum(self._table.columnWidth(c) for c in range(count)
                     if c != _COL_NAME)
        target = vp - others
        if target >= self._NAME_MIN:
            if target != self._table.columnWidth(_COL_NAME):
                hdr.resizeSection(_COL_NAME, target)
            return
        hdr.resizeSection(_COL_NAME, self._NAME_MIN)
        deficit = (self._NAME_MIN + others) - vp
        mins = self._col_mins(self._column_names())
        for c in reversed(range(count)):
            if deficit <= 0:
                break
            if c == _COL_NAME:
                continue
            room = self._table.columnWidth(c) - mins.get(c, self._COL_MIN_DEFAULT)
            if room <= 0:
                continue
            take = min(room, deficit)
            hdr.resizeSection(c, self._table.columnWidth(c) - take)
            deficit -= take

    def _sort_key(self, col: int, row: dict):
        """Sort value for *row* under column *col*. Subclasses extend."""
        if col == _COL_SOURCE:
            return row.get("source", "nexus")
        if col == _COL_VERSION:
            # Thunderstore rows have no file id, so they sort by their pin -
            # but the key must stay ONE comparable type across the whole list
            # or a mixed profile raises TypeError mid-sort. Tuple: TS rows
            # group after Nexus rows, each ordered within its own space.
            if row.get("source") == "thunderstore":
                return (1, 0, row.get("ts_version") or "")
            return (0, row.get("file_id") or 0, "")
        if col == _COL_FOMOD:
            return (bool(row.get("has_fomod")),
                    bool(row.get("fomod_export", True)))
        if col == _COL_OPTIONAL:
            return bool(row.get("optional"))
        if col == self._EDITS_COL:
            return bool(row.get("save_edits"))
        return row["name"].lower()

    def _on_header_clicked(self, col: int):
        if col == self._sort_col:
            self._sort_asc = not self._sort_asc
        else:
            self._sort_col = col
            self._sort_asc = True
        self._table.horizontalHeader().setSortIndicator(
            col, Qt.AscendingOrder if self._sort_asc else Qt.DescendingOrder)
        self._save_column_state()
        self._apply_filter()

    def _on_section_resized(self, *_args):
        if getattr(self, "_restoring_columns", False):
            return
        # Coalesce the per-pixel drag into one write when the drag settles.
        timer = getattr(self, "_col_save_timer", None)
        if timer is None:
            timer = QTimer(self)
            timer.setSingleShot(True)
            timer.timeout.connect(self._save_column_state)
            self._col_save_timer = timer
        timer.start(400)

    def _save_column_state(self):
        names = self._column_names()
        widths = {name: self._table.columnWidth(col)
                  for col, name in enumerate(names)}
        sort_name = names[self._sort_col] if 0 <= self._sort_col < len(names) else None
        try:
            column_state.save_state(widths, names, set(), sort_name,
                                    self._sort_asc,
                                    section=self._COLUMN_STATE_SECTION)
        except Exception:
            pass

    # -- data ---------------------------------------------------------------
    def _profile_dir(self) -> Path | None:
        pd = getattr(self._game, "_active_profile_dir", None) if self._game else None
        return Path(pd) if pd else None

    def _workshop_dir(self) -> Path | None:
        pd = self._profile_dir()
        return (pd / "workshop") if pd else None

    def _load_rows(self):
        """Build export rows from the active profile's modlist, then seed and
        auto-load saved settings.

        Rows come out LOWEST priority first: modlist.txt stores index 0 as the
        highest priority, and the manifest wants the winner last, so the list
        is reversed here. Separators are dropped (mirrors the Tk _on_workshop
        prep)."""
        from Utils.modlist import read_modlist
        # Ticks index into _all_rows, so they can't outlive it being rebuilt.
        self._clear_checks()
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
        # Defaults → subclass seeding → the user's saved settings, so an
        # explicit save always wins over anything inferred.
        self._seed_rows()
        self._auto_load_latest()
        # Last: rows still on the "nexus" default that can't export as Nexus
        # are switched to one that works. After the saved settings on purpose,
        # so an autosave that recorded the impossible state is corrected.
        profile_export.apply_source_defaults(
            self._all_rows, ignore_disabled=self._IGNORE_DISABLED_ROWS)

    def _seed_rows(self):
        """Hook: pre-fill row settings from another source. Default: nothing."""

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
        # Name is the tiebreaker so equal keys (checkboxes, phases) stay stable.
        rows.sort(key=lambda r: r["name"].lower())
        if self._sort_col != _COL_NAME or not self._sort_asc:
            rows.sort(key=lambda r: self._sort_key(self._sort_col, r),
                      reverse=not self._sort_asc)
        self._rows = rows
        self._rebuild_table()
        # Counts/labels track the tick set, which survives filtering.
        self._update_bulk_actions()

    # A rebuild used to recreate every cell widget (~6 per row), which made
    # each sort/filter/search a multi-hundred-ms stall at 150 mods. Widgets are
    # now created once per visible row position and only restyled on rebuilds:
    # items for every row and the first _FILL_SYNC rows' widgets are built
    # synchronously, the rest stream in per event-loop tick, so the tab and
    # every re-sort render immediately.
    _FILL_SYNC = 48
    _FILL_CHUNK = 32

    def _rebuild_table(self):
        t = self._table
        n = len(self._rows)
        # Identity-keyed position map - list.index() would deep-compare dicts
        # per row (O(n²)) and pick the wrong row for structural duplicates.
        pos = {id(r): i for i, r in enumerate(self._all_rows)}
        self._vis_data_idx = [pos[id(r)] for r in self._rows]
        t.setUpdatesEnabled(False)
        try:
            if n != t.rowCount():
                # Shrinking destroys the dropped rows' items + cell widgets,
                # so the pools must forget them too.
                t.setRowCount(n)
                if n < len(self._row_items):
                    del self._row_items[n:]
                if n < len(self._row_pool):
                    del self._row_pool[n:]
            while len(self._row_items) < n:
                self._row_items.append(
                    self._create_row_items(len(self._row_items)))
            for i, row in enumerate(self._rows):
                self._update_row_items(i, row)
            filled = len(self._row_pool)
            for i in range(filled):
                self._update_row_cells(i, self._rows[i])
            for i in range(filled, min(filled + self._FILL_SYNC, n)):
                self._row_pool.append(self._create_row_cells(i))
                self._update_row_cells(i, self._rows[i])
        finally:
            t.setUpdatesEnabled(True)
        if len(self._row_pool) < n:
            self._fill_timer.start(0)

    def _fill_more_rows(self):
        """Create the next chunk of pooled cell widgets (deferred first build)."""
        t = self._table
        n = len(self._rows)
        start = len(self._row_pool)
        if start >= n:
            return
        t.setUpdatesEnabled(False)
        try:
            for i in range(start, min(start + self._FILL_CHUNK, n)):
                self._row_pool.append(self._create_row_cells(i))
                self._update_row_cells(i, self._rows[i])
        finally:
            t.setUpdatesEnabled(True)
        if len(self._row_pool) < n:
            self._fill_timer.start(0)

    def _create_row_items(self, vi: int) -> dict:
        """Create visible row *vi*'s QTableWidgetItems (once per position)."""
        t = self._table
        # Selection tick. Deliberately NOT ItemIsUserCheckable: Qt paints
        # the indicator for any item carrying CheckStateRole, and leaving
        # the flag off means Qt never toggles it behind our back - every
        # change goes through _toggle_check, which owns the shift anchor.
        chk = QTableWidgetItem()
        chk.setFlags(Qt.ItemIsEnabled)
        chk.setTextAlignment(Qt.AlignCenter)
        t.setItem(vi, _COL_CHECK, chk)

        name_item = QTableWidgetItem()
        name_item.setFlags(Qt.ItemIsEnabled)
        t.setItem(vi, _COL_NAME, name_item)

        # Dash items sit UNDER the fomod/edits checkbox cell widgets; when a
        # row has no toggle there, the widget is hidden and the dash shows.
        dashes = {}
        for col_key, col in (("fomod_dash", _COL_FOMOD),
                             ("edits_dash", self._EDITS_COL)):
            dash = QTableWidgetItem()
            dash.setFlags(Qt.ItemIsEnabled)
            dash.setTextAlignment(Qt.AlignCenter)
            t.setItem(vi, col, dash)
            dashes[col_key] = dash
        return {"check": chk, "name": name_item, **dashes}

    def _update_row_items(self, vi: int, row: dict):
        """Restyle row *vi*'s items for the data row now shown there."""
        w = self._row_items[vi]
        data_idx = self._vis_data_idx[vi]
        w["check"].setCheckState(Qt.Checked if data_idx in self._checked
                                 else Qt.Unchecked)
        w["name"].setText(row["name"])
        w["fomod_dash"].setText("" if row.get("has_fomod") else "-")
        w["edits_dash"].setText("-" if row.get("source") == "bundle" else "")

    def _create_row_cells(self, vi: int) -> dict:
        """Create visible row *vi*'s cell widgets (once per position)."""
        t = self._table
        # Handlers resolve which data row sits at this position at CLICK time,
        # so rebuilds only have to update _vis_data_idx, never reconnect.
        di = lambda: self._vis_data_idx[vi]

        src_btn = QPushButton()
        src_btn.setCursor(Qt.PointingHandCursor)
        src_btn.clicked.connect(lambda _=False: self._pick_source(di()))
        t.setCellWidget(vi, _COL_SOURCE, src_btn)

        ver_btn = QPushButton()
        ver_btn.setCursor(Qt.PointingHandCursor)
        ver_btn.clicked.connect(lambda _=False: self._pick_version(di()))
        t.setCellWidget(vi, _COL_VERSION, ver_btn)

        fomod_wrap = self._center_checkbox(
            lambda ch: self._set_fomod(di(), ch))
        t.setCellWidget(vi, _COL_FOMOD, fomod_wrap)

        opt_wrap = self._center_checkbox(
            lambda ch: self._set_optional(di(), ch))
        t.setCellWidget(vi, _COL_OPTIONAL, opt_wrap)

        edits_wrap = self._center_checkbox(
            lambda ch: self._set_save_edits(di(), ch))
        edits_wrap._chk.setToolTip(self.tr(
            "Ship your local changes to this mod's files as binary "
            "patches, so users get the mod exactly as you have it."))
        t.setCellWidget(vi, self._EDITS_COL, edits_wrap)

        return {"src": src_btn, "ver": ver_btn, "fomod_wrap": fomod_wrap,
                "opt_wrap": opt_wrap, "edits_wrap": edits_wrap}

    def _update_row_cells(self, vi: int, row: dict):
        """Restyle row *vi*'s cell widgets for the data row now shown there."""
        w = self._row_pool[vi]
        src = row.get("source", "nexus")
        btn = w["src"]
        btn.setText(self.tr(_SOURCE_LABELS.get(src, "Nexus")))
        # Re-applying a per-widget stylesheet forces a repolish; skip it when
        # the source (and so the colour) hasn't changed.
        if getattr(btn, "_amm_src", None) != src:
            btn.setStyleSheet(_source_button_qss(src))
            btn._amm_src = src
        # A Thunderstore row's version is its package pin, not a Nexus
        # "fileid - version" label.
        if src == "thunderstore":
            w["ver"].setText(row.get("ts_version") or "-")
        else:
            w["ver"].setText(row.get("ver_label", "-"))
        self._set_check_cell(w["fomod_wrap"], bool(row.get("has_fomod")),
                             bool(row.get("fomod_export", True)))
        self._set_check_cell(w["opt_wrap"], True,
                             bool(row.get("optional", False)))
        self._set_check_cell(w["edits_wrap"], src != "bundle",
                             bool(row.get("save_edits", False)))

    def _center_checkbox(self, on_toggle) -> QWidget:
        wrap = QWidget()
        lay = QHBoxLayout(wrap)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setAlignment(Qt.AlignCenter)
        chk = QCheckBox()
        chk.toggled.connect(on_toggle)
        lay.addWidget(chk)
        wrap._chk = chk
        return wrap

    @staticmethod
    def _set_check_cell(wrap: QWidget, shown: bool, checked: bool):
        """Set a pooled checkbox cell's state without firing its handler."""
        chk = wrap._chk
        chk.blockSignals(True)
        chk.setChecked(checked)
        chk.blockSignals(False)
        wrap.setVisible(shown)

    # -- cell actions -------------------------------------------------------
    def _set_optional(self, data_idx: int, checked: bool):
        self._all_rows[data_idx]["optional"] = bool(checked)

    def _set_fomod(self, data_idx: int, checked: bool):
        self._all_rows[data_idx]["fomod_export"] = bool(checked)

    def _set_save_edits(self, data_idx: int, checked: bool):
        self._all_rows[data_idx]["save_edits"] = bool(checked)

    def _bundle_blocked_reason(self, row: dict) -> str:
        """Why *row* may not be bundled, or "" when bundling is fine.

        A personal ``.amethyst`` export is a backup of the user's own setup, so
        anything may be packed into it. Publishing is different - see the
        override in CreateCollectionView.
        """
        return ""

    def _thunderstore_blocked_reason(self, row: dict) -> str:
        """Why *row* may not use the Thunderstore source, or "" when it may.

        Only a mod carrying a Thunderstore pin can be exported as one - there
        is nothing to download otherwise.
        """
        if row.get("ts_namespace") and row.get("ts_name"):
            return ""
        return self.tr("This mod was not installed from Thunderstore.")

    def _pick_source(self, data_idx: int):
        row = self._all_rows[data_idx]
        notes: dict = {}
        disabled: set = set()
        ts_blocked = self._thunderstore_blocked_reason(row)
        if ts_blocked:
            notes["thunderstore"] = ts_blocked
            disabled.add("thunderstore")

        def _picked(source, url, instructions, di=data_idx):
            r = self._all_rows[di]
            r["source"] = source
            r["direct_url"] = url
            r["source_instructions"] = instructions
            self._refresh_visible_row(di)

        _SourceOverlay(self.window(), row["name"], row.get("source", "nexus"),
                       row.get("direct_url", ""), _picked,
                       current_instructions=row.get("source_instructions", ""),
                       bundle_blocked_reason=self._bundle_blocked_reason(row),
                       option_notes=notes, disabled_sources=disabled,
                       allowed_sources=self._ALLOWED_SOURCES)

    def _pick_version(self, data_idx: int):
        row = self._all_rows[data_idx]
        if row.get("source") == "thunderstore":
            # Thunderstore's version list is public - no API object needed.
            if not row.get("ts_versions_fetched") and row.get("ts_namespace"):
                row["ts_versions_fetched"] = True
                row["versions_fetching"] = True
                threading.Thread(
                    target=self._fetch_ts_versions, args=(data_idx,),
                    daemon=True, name="export-ts-versions").start()
            self._open_version_dialog(data_idx)
            return
        # Lazily fetch the file list on first open (Nexus mods only). The card
        # opens straight away and is filled in by _on_versions_ready, so the
        # first open shows every version rather than just the installed one.
        if (not row.get("versions_fetched") and row.get("mod_id")
                and self._api is not None and row.get("source", "nexus") == "nexus"):
            row["versions_fetched"] = True
            row["versions_fetching"] = True
            threading.Thread(
                target=self._fetch_versions, args=(data_idx,),
                daemon=True, name="export-versions").start()
        self._open_version_dialog(data_idx)

    def _open_version_dialog(self, data_idx: int):
        row = self._all_rows[data_idx]
        is_ts = row.get("source") == "thunderstore"
        if is_ts:
            current = row.get("ts_version") or "-"
            options = row.get("ts_ver_options") or [
                {"label": current, "name": row.get("ts_full_name", ""),
                 "size_bytes": row.get("ts_size_bytes") or 0}]
        else:
            current = row.get("ver_label", "-")
            options = row.get("ver_options") or [
                {"label": current, "name": "", "size_bytes": 0}]

        def _picked(sel, di=data_idx):
            r = self._all_rows[di]
            if r.get("source") == "thunderstore":
                # A Thunderstore pin is (namespace, name, version) - it shares
                # nothing with the Nexus "fileid - version" label, so leave
                # file_id/ver_label untouched.
                version = sel.get("label") or r.get("ts_version", "")
                r["ts_version"] = version
                r["ts_full_name"] = (
                    sel.get("name")
                    or f"{r.get('ts_namespace', '')}-{r.get('ts_name', '')}"
                       f"-{version}")
                r["ts_size_bytes"] = sel.get("size_bytes", 0) or 0
                self._refresh_visible_row(di)
                return
            r["ver_label"] = sel.get("label", r["ver_label"])
            r["size_bytes"] = sel.get("size_bytes", 0)
            try:
                r["file_id"] = profile_export.split_ver_label(
                    r["ver_label"])[0] or r["file_id"]
            except (ValueError, IndexError):
                pass
            self._refresh_visible_row(di)

        overlay = _VersionOverlay(
            self.window(), row["name"], options, current,
            _picked, loading=bool(row.get("versions_fetching")))
        # Remembered so an in-flight fetch can populate this very card.
        self._version_overlay = (data_idx, overlay)

    def _fetch_ts_versions(self, data_idx: int):
        """Worker thread: fetch a Thunderstore package's version list, marshal
        back via the same Signal the Nexus fetch uses (never touch widgets
        off-thread)."""
        row = self._all_rows[data_idx]
        options = []
        try:
            from Thunderstore.thunderstore_api import fetch_versions
            ns = row.get("ts_namespace", "")
            pkg = row.get("ts_name", "")
            for v in fetch_versions(ns, pkg) or []:
                if not isinstance(v, dict):
                    continue
                version = (v.get("version_number") or "").strip()
                if not version:
                    continue
                try:
                    size = int(v.get("file_size") or v.get("size") or 0)
                except (TypeError, ValueError):
                    size = 0
                options.append({
                    "label": version,
                    "name": f"{ns}-{pkg}-{version}",
                    "size_bytes": size,
                })
        except Exception:
            options = []
        safe_emit(self._versions_ready, data_idx, options)

    def _fetch_versions(self, data_idx: int):
        """Worker thread: fetch the mod's file list from Nexus, marshal back via a
        Signal (never touch widgets off-thread)."""
        row = self._all_rows[data_idx]
        try:
            domain = (row.get("game_domain") or self._game_domain).strip()
            result = self._api.get_mod_files(domain, row["mod_id"])
            files = result.files if result else []
        except Exception:
            files = []
        sorted_files = sorted(files, key=lambda f: f.uploaded_timestamp, reverse=True)
        options = [
            {
                "label": f"{f.file_id}{profile_export.VER_LABEL_SEP}{f.version}",
                "name": f.name,
                "size_bytes": (f.size_in_bytes if f.size_in_bytes is not None
                               else (f.size_kb or 0) * 1024),
            }
            for f in sorted_files if f.file_id
        ]
        safe_emit(self._versions_ready, data_idx, options)

    def _on_versions_ready(self, data_idx: int, options):
        row = self._all_rows[data_idx]
        is_ts = row.get("source") == "thunderstore"
        row["versions_fetching"] = False
        open_idx, overlay = getattr(self, "_version_overlay", None) or (None, None)
        showing = (overlay is not None and open_idx == data_idx
                   and not getattr(overlay, "_done", True))
        if not options:
            # Let a later open retry rather than caching the failure forever.
            if is_ts:
                row["ts_versions_fetched"] = False
            else:
                row["versions_fetched"] = False
            if showing:
                overlay.set_loading(False, self.tr(
                    "Could not load versions from Thunderstore.") if is_ts
                    else self.tr("Could not load versions from Nexus."))
            return
        if is_ts:
            # The installed pin is already correct and the rest of this method
            # is Nexus file-id bookkeeping, so a Thunderstore row only gains
            # the list to choose from - never an auto-selected version.
            row["ts_ver_options"] = options
            if showing:
                overlay.set_loading(False)
                overlay.set_options(options, row.get("ts_version") or "-")
            return
        row["ver_options"] = options
        # Only auto-select if the current label is still a placeholder - opening the
        # picker must not silently change the file_id (port of _apply_versions).
        cur_label = row["ver_label"]
        # A real label parses as "<file id> <sep> <version>"; anything else
        # (the "-" dash, a bare id) is a placeholder we may fill in.
        is_placeholder = not profile_export.split_ver_label(cur_label)[0]
        if is_placeholder:
            # Match by parsed file id, not label prefix - labels use the
            # em-dash VER_LABEL_SEP, so a "id -" string match never hits.
            preferred = int(row.get("file_id") or 0)
            matched = next(
                (o for o in options
                 if profile_export.split_ver_label(o["label"])[0] == preferred),
                None) if preferred else None
            # A row with a real file_id that Nexus no longer lists must KEEP
            # it: defaulting to options[0] would silently re-pin the mod to
            # the newest upload. Only a row with no identity at all takes the
            # newest as its starting point.
            selected = matched or (None if preferred else options[0])
            if selected is not None:
                row["ver_label"] = selected["label"]
                row["size_bytes"] = selected["size_bytes"]
                try:
                    row["file_id"] = profile_export.split_ver_label(
                        selected["label"])[0] or row["file_id"]
                except (ValueError, IndexError):
                    pass
                self._refresh_visible_row(data_idx)
        # Fill the open card LAST, so it highlights the settled selection.
        if showing:
            overlay.set_loading(False)
            overlay.set_options(options, row.get("ver_label"))

    def _refresh_visible_row(self, data_idx: int):
        """Update the on-screen widgets for one data row if it's currently visible."""
        row = self._all_rows[data_idx]
        # Identity match - == would deep-compare dicts and can hit a
        # structural duplicate.
        vis_idx = next((i for i, r in enumerate(self._rows) if r is row), None)
        if vis_idx is None:
            return
        if vis_idx < len(self._row_items):
            self._update_row_items(vis_idx, row)
        if vis_idx < len(self._row_pool):
            self._update_row_cells(vis_idx, row)

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

        # Bundling a mod that has a Nexus page packs someone else's work into
        # the file instead of linking to it. Allowed here - a .amethyst is a
        # personal backup and never leaves this machine on its own - but the
        # user should know before handing the file to anyone else.
        redistributable = profile_export.redistributable_bundle_names(
            self._all_rows)
        if redistributable:
            from gui_qt.confirm_overlay import ConfirmOverlay

            def _confirmed(ok: bool):
                if ok:
                    self._archive_preflight(self._open_export_picker)

            count = len(redistributable)
            body = self.tr(
                "{0} bundled mod(s) below are available on Nexus, so their "
                "files will be packed into this export rather than downloaded "
                "on import.\n\nThat is fine for your own backup. Do not share "
                "or upload the file in this state - set those mods to Nexus "
                "and use Edits to carry your local changes."
            ).format(count)
            ConfirmOverlay(self.window(), self.tr("Bundled Nexus mods"), body,
                           _confirmed, confirm_label=self.tr("Export anyway"),
                           cancel_label=self.tr("Cancel"), danger=False,
                           list_items=redistributable)
            return

        self._archive_preflight(self._open_export_picker)

    # -- missing-archive preflight ------------------------------------------
    def _archive_preflight(self, proceed):
        """Check for export features whose original archive is gone from the
        download cache, offering a redownload before continuing. *proceed*
        resumes the export flow (possibly from an overlay callback)."""
        rows = self._snapshot_rows()
        game = self._game

        def _worker():
            try:
                from Utils.collection_export import missing_archive_report
                report = missing_archive_report(
                    rows, game,
                    check_fomod=self._ARCHIVE_PREFLIGHT_FOMOD,
                    include_disabled=not self._IGNORE_DISABLED_ROWS)
            except Exception as exc:
                self._log(f"[export] archive preflight failed: {exc}")
                report = []
            safe_emit(self._archive_check_done, report, proceed)

        threading.Thread(target=_worker, daemon=True,
                         name="export-archive-preflight").start()

    def _on_archive_check_done(self, report, proceed):
        if not report:
            proceed()
            return
        from gui_qt.missing_archives_overlay import MissingArchivesOverlay
        MissingArchivesOverlay.show_over(
            self.window(), report, api=self._api,
            game_name=self._game.name if self._game else "",
            log_fn=self._log,
            on_done=lambda ok: proceed() if ok else None)

    def _open_export_picker(self):
        """Ask for the output path, then export. Split out of :meth:`_on_export`
        so the bundled-Nexus-mods confirmation can resume it."""
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
        # Immediate feedback (indeterminate) - the worker switches the card to a
        # determinate byte bar once the packaging file list is known.
        self._on_export_progress(0, 0, self.tr("Preparing export…"))
        # Prefetch missing sizes + write on a worker thread.
        threading.Thread(
            target=self._export_worker, args=(str(path),),
            daemon=True, name="export-profile").start()

    def _export_worker(self, out_path: str):
        """Worker thread, phase 1: prefetch missing file sizes into a LOCAL
        list - never mutating _all_rows, which the UI thread reads/sorts - and
        post them back via _sizes_ready; the UI thread applies them and starts
        the packaging worker."""
        # Prefetch file sizes for nexus rows that have mod_id + file_id but no
        # size yet (single batched GraphQL request - port of _prefetch_sizes).
        needs_size = [
            (i, (r.get("game_domain") or self._game_domain).strip(),
             r["mod_id"], r["file_id"])
            for i, r in enumerate(self._all_rows)
            if r.get("mod_id") and r.get("file_id") and not r.get("size_bytes")
            and r.get("source", "nexus") == "nexus"
        ]
        updates: list[tuple[int, int]] = []
        if needs_size and self._api is not None:
            grouped: dict[str, list[tuple[int, int, int]]] = {}
            for i, domain, mod_id, file_id in needs_size:
                grouped.setdefault(domain, []).append((i, mod_id, file_id))
            for domain, rows in grouped.items():
                pairs = [(mod_id, file_id) for _i, mod_id, file_id in rows]
                try:
                    size_map = self._api.graphql_file_sizes_batch(domain, pairs)
                except Exception:
                    size_map = {}
                for i, mod_id, file_id in rows:
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
            target=self._package_worker,
            args=(str(out_path), self._snapshot_rows()),
            daemon=True, name="export-package").start()

    def _snapshot_rows(self) -> list:
        """A detached copy of the export rows for a worker thread - the table
        stays interactive during packing, so workers must not read the live
        dicts the UI is still mutating."""
        return [dict(r) for r in self._all_rows]

    def _package_worker(self, out_path: str, rows=None):
        """Worker thread, phase 2: build the manifest and write the archive."""
        rows = rows if rows is not None else self._all_rows
        try:
            try:
                from version import __version__ as app_version
            except Exception:
                app_version = ""

            game_name = self._game.name if self._game else None
            pd = self._profile_dir()
            manifest = profile_export.build_manifest(
                rows, self._game_domain, app_version,
                game_name=game_name, profile_dir=pd)

            staging_root = (self._game.get_effective_mod_staging_path()
                            if self._game else None)
            overwrite_root = (self._game.get_effective_overwrite_path()
                              if self._game else None)
            bundle_names = [r["name"] for r in rows
                            if r.get("source") == "bundle"]

            # Local file edits → binary patches applied over the pristine
            # download on import (diffing extracts archives, so it can take a
            # moment - announce it on the indeterminate bar).
            scratch: list = []
            try:
                if any(r.get("save_edits") for r in rows):
                    safe_emit(self._export_progress, 0, 0,
                              self.tr("Building file-edit patches…"))
                patch_jobs, patch_warnings = profile_export.build_patch_jobs(
                    rows, manifest, self._game, scratch_out=scratch)
                for warning in patch_warnings:
                    self._log(f"[export] {warning}")

                final = profile_export.write_amethyst(
                    out_path, manifest,
                    staging_root=staging_root, overwrite_root=overwrite_root,
                    profile_dir=pd, bundle_names=bundle_names,
                    patch_jobs=patch_jobs,
                    progress_cb=self._make_progress_cb())
            finally:
                import shutil
                for tmp in scratch:
                    shutil.rmtree(tmp, ignore_errors=True)
            safe_emit(self._export_done, True, str(final))
        except Exception as exc:
            safe_emit(self._export_done, False, str(exc))

    def _make_progress_cb(self):
        """Throttled bridge from write_amethyst's per-file callback (worker
        thread) to the progress signal - at most ~10 emits/s, but the final
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
            elif parts and parts[0] == "patches":
                phase = self.tr("Packing file-edit patches…")
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
