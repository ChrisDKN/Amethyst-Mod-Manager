"""Settings modal opened from the top-bar gear button.

The settings UI is a dimmed, in-window modal rather than a detachable or
panel-scoped tab. Six tabs group the existing settings into Appearance,
Downloads, General, Paths, Advanced and About while keeping every setting's
existing save-on-change behaviour.

Save-on-change: every control writes straight to amethyst.ini through the
toolkit-free `Utils.ui_config` load_*/save_* helpers the moment it changes - there
is no Save/Cancel button. Language and UI Scale take effect on restart; themes
are applied to the running Qt application immediately.

A curated subset of the Tk Settings panel (gui/status_bar.py `SettingsPanel`):
User Interface (incl. Theme + UI Scale), Archives, Downloads, Extraction,
General, Paths - plus a Manage Caches action. Theme colour pickers are
intentionally omitted (Qt has no colour-override system yet).

Option descriptions are tooltips (on the row and on its accent "?" marker), not
inline labels - rows stay one line tall and nothing clips at narrow widths.

Layout contract for every section (see :meth:`_section`):

    col 0                col 1
    label / checkbox     control (combo, slider, path row, …)

so controls start at a common x down the whole page. The "?" marker is not a
column - it rides inside each row's own layout, immediately after the widget it
describes, which keeps it visibly attached to that option.

Action buttons never sit between options: `_action_row` queues them into a
per-section footer that `_finish_section` flushes at the bottom of the group.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal, QRectF, QSize
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QScrollArea, QFrame,
    QLabel, QCheckBox, QComboBox, QSlider, QLineEdit, QPushButton, QGroupBox,
    QApplication, QTabWidget, QAbstractButton, QSizePolicy,
)

from gui_qt.theme_qt import active_palette, _c
from gui_qt.help_marker import tip_text, make_help_marker, help_mark_qss
from gui_qt.wheel_guard import no_wheel
from gui_qt.flow_layout import FlowLayout, enable_height_for_width
from gui_qt.overlay_base import OverlayBase
from Utils import ui_config as uc


# ---------------------------------------------------------------------------
def _palette_colour(palette: dict, key: str, fallback: str) -> QColor:
    """Return a concrete QColor from a theme palette role."""
    value = palette.get(key, fallback)
    if isinstance(value, (tuple, list)):
        value = value[-1]
    colour = QColor(str(value))
    return colour if colour.isValid() else QColor(fallback)


class _ThemePreviewButton(QAbstractButton):
    """Keyboard-accessible square that paints a tiny sample of one theme."""

    TILE_SIZE = 64

    def __init__(self, theme_id: str, display_name: str, palette: dict,
                 parent=None):
        super().__init__(parent)
        self.theme_id = theme_id
        self._theme_palette = dict(palette)
        self.setCheckable(True)
        self.setCursor(Qt.PointingHandCursor)
        self.setFixedSize(self.TILE_SIZE, self.TILE_SIZE)
        self.setToolTip(display_name)
        self.setAccessibleName(display_name)
        self.setAccessibleDescription(self.tr("Theme preview"))

    def sizeHint(self) -> QSize:  # noqa: N802 - Qt virtual
        return QSize(self.TILE_SIZE, self.TILE_SIZE)

    def paintEvent(self, _event):  # noqa: N802 - Qt virtual
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        pal = self._theme_palette
        deep = _palette_colour(pal, "BG_DEEP", "#171717")
        panel = _palette_colour(pal, "BG_PANEL", "#242424")
        header = _palette_colour(pal, "BG_HEADER", "#303030")
        row = _palette_colour(pal, "BG_ROW", "#383838")
        text = _palette_colour(pal, "TEXT_MAIN", "#eeeeee")
        dim = _palette_colour(pal, "TEXT_DIM", "#999999")
        accent = _palette_colour(pal, "ACCENT", "#0078d4")
        border = _palette_colour(pal, "BORDER", "#555555")

        outer = QRectF(1.5, 1.5, self.width() - 3, self.height() - 3)
        p.setBrush(deep)
        p.setPen(QPen(accent if self.isChecked() else border,
                      3.0 if self.isChecked() else 1.0))
        p.drawRoundedRect(outer, 7, 7)

        # A tiny but recognisable app: raised panel, header, list rows, accent
        # selection and short foreground strokes. It is intentionally painted
        # from semantic roles so custom themes need no preview image asset.
        content = QRectF(7, 7, self.width() - 14, self.height() - 14)
        p.setPen(Qt.NoPen)
        p.setBrush(panel)
        p.drawRoundedRect(content, 4, 4)
        p.setBrush(header)
        p.drawRoundedRect(QRectF(7, 7, self.width() - 14, 11), 4, 4)
        p.drawRect(QRectF(7, 13, self.width() - 14, 5))

        p.setBrush(row)
        p.drawRoundedRect(QRectF(11, 23, self.width() - 22, 9), 2, 2)
        p.setBrush(accent)
        p.drawRoundedRect(QRectF(11, 36, self.width() - 22, 9), 2, 2)
        p.setBrush(row)
        p.drawRoundedRect(QRectF(11, 49, self.width() - 22, 7), 2, 2)

        p.setPen(QPen(text, 1.6, Qt.SolidLine, Qt.RoundCap))
        p.drawLine(15, 27, 34, 27)
        p.setPen(QPen(dim, 1.4, Qt.SolidLine, Qt.RoundCap))
        p.drawLine(15, 52, 39, 52)

        if self.isChecked():
            # Compact active-state tick; contrast by drawing a dark shadow
            # under the light stroke so it reads on both bright/dark accents.
            p.setPen(QPen(deep, 3.2, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
            p.drawLine(46, 12, 49, 15)
            p.drawLine(49, 15, 55, 9)
            p.setPen(QPen(text, 1.7, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
            p.drawLine(46, 12, 49, 15)
            p.drawLine(49, 15, 55, 9)

        if self.hasFocus():
            focus_pen = QPen(text, 1.0, Qt.DashLine)
            p.setBrush(Qt.NoBrush)
            p.setPen(focus_pen)
            p.drawRoundedRect(outer.adjusted(3, 3, -3, -3), 5, 5)


# ---------------------------------------------------------------------------
class SettingsView(OverlayBase):
    """Save-on-change settings presented as a single in-window modal."""

    CARD_W = 700
    CARD_H = 700
    MIN_W = 600
    MIN_H = 420
    CLICK_OUTSIDE_CANCELS = True

    # Carries the cache-size scan result from a daemon worker thread to the UI
    # pick_folder's callback fires on the portal WORKER thread; marshal the
    # (edit, save_fn, path) result to the GUI thread before touching a widget.
    _folder_picked = Signal(object)

    # Stable width for the language selector within the common settings grid.
    COMBO_W = 180

    def __init__(self, window, on_closed=None):
        super().__init__(window, on_done=on_closed)
        self.setFocusPolicy(Qt.StrongFocus)
        self._window = window          # main window - for _notify, threads
        self._pal = active_palette()
        # id(grid) -> [(button, help, extra), ...]; see _action_row.
        self._pending_actions: dict[int, list] = {}
        self._folder_picked.connect(self._on_folder_picked)

        # Collection settings - read once here; both the Downloads and the
        # Extraction sections persist through this shared dict.
        try:
            cs = uc.load_collection_settings()
        except Exception:
            cs = {}
        self._cs = {
            "max_concurrent": int(cs.get("max_concurrent", uc._DEFAULT_MAX_CONCURRENT)),
            "max_extract_workers": int(cs.get("max_extract_workers", uc._DEFAULT_MAX_EXTRACT_WORKERS)),
            "check_download_locations": bool(cs.get("check_download_locations", True)),
            "clear_archive_after_install": bool(cs.get("clear_archive_after_install", False)),
        }

        _card, outer = self._make_card(
            "SettingsCard", margins=(0, 0, 0, 0), spacing=0,
            bg_key="BG_DEEP")
        self._build_toolbar(outer)

        self._tabs = QTabWidget()
        self._tabs.setObjectName("SettingsTabs")
        self._tabs.setDocumentMode(True)
        self._tabs.tabBar().setExpanding(False)
        outer.addWidget(self._tabs, 1)

        self._add_tab(self.tr("Appearance"), self._build_user_interface)
        self._add_tab(
            self.tr("Downloads"), self._build_archives,
            self._build_downloads, self._build_extraction)
        self._add_tab(self.tr("General"), self._build_general)
        self._add_tab(self.tr("Paths"), self._build_paths)
        self._add_tab(self.tr("Advanced"), self._build_advanced)
        self._add_tab(self.tr("About"), self._build_system_info)
        self._tabs.setCurrentIndex(0)

        self.setStyleSheet(self._qss())
        self._present()
        self.setFocus(Qt.OtherFocusReason)

    @classmethod
    def show_over(cls, host, on_closed=None):
        top = host.window() if host is not None else None
        return cls(top or host, on_closed=on_closed)

    def _build_toolbar(self, outer: QVBoxLayout) -> None:
        bar = QFrame()
        bar.setObjectName("SettingsToolbar")
        row = QHBoxLayout(bar)
        row.setContentsMargins(16, 10, 10, 10)
        row.setSpacing(8)
        title = QLabel(self.tr("Settings"))
        title.setObjectName("SettingsTitle")
        row.addWidget(title)
        row.addStretch(1)
        close = QPushButton(self.tr("Close"))
        close.setObjectName("FormButton")
        close.setCursor(Qt.PointingHandCursor)
        close.clicked.connect(self._finish)
        row.addWidget(close)
        outer.addWidget(bar)

    def _add_tab(self, label: str, *builders) -> None:
        """Build one independently scrollable settings tab."""
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)

        body = QWidget()
        body.setObjectName("SettingsPage")
        self._v = QVBoxLayout(body)
        self._v.setContentsMargins(16, 14, 16, 18)
        self._v.setSpacing(14)
        for build in builders:
            build()
        self._v.addStretch(1)
        scroll.setWidget(body)
        self._tabs.addTab(scroll, label)

    # ---- styling ----------------------------------------------------------
    def _qss(self) -> str:
        c = lambda k: _c(self._pal, k)
        return f"""
        #SettingsToolbar {{
            background: {c('BG_HEADER')};
            border-bottom: 1px solid {c('BORDER')};
            border-top-left-radius: 8px;
            border-top-right-radius: 8px;
        }}
        #SettingsTitle {{
            color: {c('TEXT_MAIN')};
            font-size: 17px;
            font-weight: 600;
        }}
        #SettingsTabs > QTabBar {{
            background: {c('BG_HEADER')};
        }}
        #SettingsPage {{ background: {c('BG_DEEP')}; }}
        QGroupBox {{
            border: 1px solid {c('BORDER')};
            border-radius: 6px;
            margin-top: 10px;
            padding: 10px 12px 12px 12px;
            background: {c('BG_PANEL')};
        }}
        QGroupBox::title {{
            subcontrol-origin: margin;
            left: 10px; padding: 0 5px;
            color: {c('TEXT_MAIN')};
            font-weight: bold;
        }}
        {help_mark_qss(self._pal)}
        QLabel#RestartNote {{ color: {c('TEXT_WARN')}; }}
        QLabel#Help {{ color: {c('TEXT_DIM')}; }}
        QSlider::groove:horizontal {{
            height: 4px; background: {c('BG_DEEP')}; border-radius: 2px;
        }}
        QSlider::handle:horizontal {{
            background: {c('ACCENT')}; width: 14px; margin: -6px 0;
            border-radius: 7px;
        }}
        QSlider::sub-page:horizontal {{ background: {c('ACCENT')}; border-radius: 2px; }}
        """

    # ---- section + control builders --------------------------------------
    # Grid columns shared by every section, so labels and controls line up down
    # the whole page. Help markers are NOT a column - each "?" lives inside its
    # own row's layout, right after the widget it describes.
    COL_LABEL = 0
    COL_CTRL = 1

    def _section(self, title: str) -> QGridLayout:
        """Add a QGroupBox and return its (label | control) QGridLayout."""
        box = QGroupBox(title)
        grid = QGridLayout(box)
        grid.setContentsMargins(10, 8, 10, 8)
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(8)
        grid.setColumnStretch(self.COL_LABEL, 0)
        grid.setColumnStretch(self.COL_CTRL, 1)
        self._v.addWidget(box)
        # Track the next free row per grid via a dynamic attribute.
        grid.setProperty("_row", 0)
        # Buttons queued by _action_row, flushed by _finish_section.
        self._pending_actions[id(grid)] = []
        return grid

    def _next_row(self, grid: QGridLayout) -> int:
        r = int(grid.property("_row") or 0)
        grid.setProperty("_row", r + 1)
        return r

    def _add_help(self, wrap: QHBoxLayout, help: str | None, *targets) -> None:
        """Tooltip `targets` and append a "?" marker to the row's own layout.

        Deliberately not a grid column: the marker belongs beside the text it
        describes, so it rides in the row layout right after the widget.
        """
        if not help:
            return
        tip = self._tip_text(help)
        for w in targets:
            if w is not None:
                w.setToolTip(tip)
        wrap.addWidget(self._help_marker(help))

    def _action_row(self, grid: QGridLayout, label: str, on_click,
                    help: str | None = None,
                    extra: QWidget | None = None) -> QPushButton:
        """Queue an action button for this section's footer.

        Buttons are collected rather than placed inline so they never split the
        run of options above them (the screenshot bug: "Edit custom
        install-name rules…" landed mid-list). :meth:`_finish_section` lays the
        whole queue out as one left-aligned row at the bottom of the group.
        """
        btn = QPushButton(label)
        btn.setCursor(Qt.PointingHandCursor)
        btn.clicked.connect(on_click)
        if help:
            btn.setToolTip(self._tip_text(help))
        self._pending_actions[id(grid)].append((btn, help, extra))
        return btn

    def _finish_section(self, grid: QGridLayout) -> None:
        """Flush queued action buttons into a footer row at the group's bottom.

        Call once per section, after every option row has been added.
        """
        pending = self._pending_actions.pop(id(grid), [])
        if not pending:
            return
        row = self._next_row(grid)
        wrap = QHBoxLayout()
        # Top margin separates the action row from the options above it, so the
        # footer reads as a distinct band rather than one more option.
        wrap.setContentsMargins(0, 6, 0, 0)
        wrap.setSpacing(8)
        for btn, help, extra in pending:
            wrap.addWidget(btn)
            if help:
                wrap.addWidget(self._help_marker(help))
            if extra is not None:
                wrap.addWidget(extra, 1)
        wrap.addStretch(1)
        holder = QWidget(); holder.setLayout(wrap)
        grid.addWidget(holder, row, self.COL_LABEL, 1, 2)

    def _tip_text(self, text: str) -> str:
        return tip_text(text)

    def _help_marker(self, text: str) -> QLabel:
        return make_help_marker(text)

    def _checkbox(self, grid: QGridLayout, label: str, load_fn, save_fn,
                  help: str | None = None, on_changed=None) -> QCheckBox:
        cb = QCheckBox(label)
        try:
            cb.setChecked(bool(load_fn()))
        except Exception:
            pass

        def _toggled(v):
            self._safe_save(save_fn, v)
            if on_changed is not None:
                try:
                    on_changed(v)
                except Exception:
                    pass

        cb.toggled.connect(_toggled)
        row = self._next_row(grid)
        if help:
            wrap = QHBoxLayout()
            wrap.setContentsMargins(0, 0, 0, 0)
            wrap.addWidget(cb)
            self._add_help(wrap, help, cb)
            wrap.addStretch(1)
            holder = QWidget(); holder.setLayout(wrap)
            grid.addWidget(holder, row, self.COL_LABEL, 1, 2)
        else:
            grid.addWidget(cb, row, self.COL_LABEL, 1, 2)
        return cb

    def _combo(self, grid: QGridLayout, label: str,
               pairs: list[tuple[str, str]], current_value: str,
               save_fn) -> QComboBox:
        """`pairs` = [(display, value), ...]; selecting saves the value."""
        row = self._next_row(grid)
        grid.addWidget(QLabel(label), row, self.COL_LABEL)
        combo = QComboBox()
        values = [v for _d, v in pairs]
        for disp, _v in pairs:
            combo.addItem(disp)
        if current_value in values:
            combo.setCurrentIndex(values.index(current_value))
        combo.currentIndexChanged.connect(
            lambda i: self._safe_save(save_fn, values[i]))
        no_wheel(combo)
        combo.setFixedWidth(self.COMBO_W)
        grid.addWidget(combo, row, self.COL_CTRL, Qt.AlignLeft)
        return combo

    def _slider(self, grid: QGridLayout, label: str, lo: int, hi: int,
                value: int, on_change, help: str | None = None) -> QSlider:
        """Integer slider lo..hi with a live value label. `on_change(int)`."""
        row = self._next_row(grid)
        lbl = QLabel(label)
        grid.addWidget(lbl, row, self.COL_LABEL)
        wrap = QHBoxLayout()
        wrap.setContentsMargins(0, 0, 0, 0)
        # Gap so a handle parked at maximum doesn't sit on top of the readout.
        wrap.setSpacing(10)
        sld = QSlider(Qt.Horizontal)
        sld.setMinimum(lo); sld.setMaximum(hi)
        sld.setValue(max(lo, min(hi, value)))
        sld.setFixedWidth(200)
        val_lbl = QLabel(str(sld.value()))
        # Fixed (not minimum) width: the readout text varies per slider
        # ("Unlimited", "All", "150%"), and a minimum width would let each one
        # size itself, staggering the "?" markers that follow it.
        val_lbl.setFixedWidth(68)
        sld.valueChanged.connect(lambda v: val_lbl.setText(str(v)))
        sld.valueChanged.connect(lambda v: on_change(v))
        no_wheel(sld)
        wrap.addWidget(sld)
        wrap.addWidget(val_lbl)
        self._add_help(wrap, help, lbl, sld)
        wrap.addStretch(1)
        holder = QWidget(); holder.setLayout(wrap)
        grid.addWidget(holder, row, self.COL_CTRL)
        return sld, val_lbl

    def _browse_row(self, grid: QGridLayout, label: str, load_fn, save_fn,
                    on_browse, help: str | None = None) -> QLineEdit:
        """Shared body of :meth:`_path_row` / :meth:`_file_row`.

        `on_browse()` opens whichever picker the caller wants; everything else
        (edit, Browse/Clear buttons, alignment) is identical between them.
        """
        row = self._next_row(grid)
        lbl = QLabel(label)
        grid.addWidget(lbl, row, self.COL_LABEL)
        wrap = QHBoxLayout()
        wrap.setContentsMargins(0, 0, 0, 0)
        edit = QLineEdit()
        try:
            edit.setText(load_fn() or "")
        except Exception:
            pass
        edit.editingFinished.connect(
            lambda: self._safe_save(save_fn, edit.text().strip()))
        browse = QPushButton(self.tr("Browse"))
        browse.setCursor(Qt.PointingHandCursor)
        browse.clicked.connect(lambda: on_browse())
        clear = QPushButton(self.tr("Clear"))
        clear.setCursor(Qt.PointingHandCursor)
        clear.clicked.connect(lambda: self._clear_path(edit, save_fn))
        # Equal widths so the Browse/Clear pair forms a straight right edge
        # down the Paths section regardless of translated label lengths.
        for b in (browse, clear):
            b.setFixedWidth(max(browse.sizeHint().width(),
                                clear.sizeHint().width()))
        wrap.addWidget(edit, 1)
        wrap.addWidget(browse)
        wrap.addWidget(clear)
        self._add_help(wrap, help, lbl, edit)
        holder = QWidget(); holder.setLayout(wrap)
        grid.addWidget(holder, row, self.COL_CTRL)
        return edit

    def _path_row(self, grid: QGridLayout, label: str, load_fn, save_fn,
                  help: str | None = None) -> QLineEdit:
        edit = self._browse_row(
            grid, label, load_fn, save_fn,
            lambda: self._browse_into(edit, save_fn, label), help=help)
        return edit

    def _file_row(self, grid: QGridLayout, label: str, load_fn, save_fn,
                  filters: "list[tuple[str, list[str]]] | None" = None,
                  help: str | None = None) -> QLineEdit:
        """Like :meth:`_path_row` but the Browse button picks a single file
        (e.g. an AppImage) instead of a folder."""
        edit = self._browse_row(
            grid, label, load_fn, save_fn,
            lambda: self._browse_file_into(edit, save_fn, label, filters),
            help=help)
        return edit

    # ---- sections ---------------------------------------------------------
    def _build_user_interface(self):
        # Put the visual choice first, matching the Appearance tab's purpose.
        # Themes persist and apply live; language and UI scale are startup-only.
        theme_group = self._section(self.tr("Theme"))
        self._build_theme(theme_group)
        self._action_row(
            theme_group, self.tr("Edit / Create Theme…"),
            self._open_theme_editor)
        self._finish_section(theme_group)

        g = self._section(self.tr("User Interface"))
        # Language row: the combo sits in the shared control column; its
        # "Sync language files" button moves to the section footer with the
        # other actions, so the option rows stay a clean label|control grid.
        row = self._next_row(g)
        g.addWidget(QLabel(self.tr("Language")), row, self.COL_LABEL)
        self._lang_combo = QComboBox()
        no_wheel(self._lang_combo)
        # Keep the selector compact instead of stretching across the modal.
        self._lang_combo.setFixedWidth(self.COMBO_W)
        g.addWidget(self._lang_combo, row, self.COL_CTRL, Qt.AlignLeft)
        self._populate_language_combo()

        self._build_ui_scale(g)

        # Theme is live; only Language / UI Scale still need a restart.
        # Indented to the control column so it reads as a note about the
        # controls above rather than a row label of its own.
        note = QLabel(self.tr(
            "Language and UI scale changes take effect after restart."))
        note.setObjectName("RestartNote")
        g.addWidget(note, self._next_row(g), self.COL_CTRL)

        self._checkbox(
            g, self.tr("Hide BSA conflicts"),
            uc.load_hide_bsa_conflicts, uc.save_hide_bsa_conflicts,
            help=self.tr("Hide BSA/BA2 archive conflict flags (also skips that "
                 "conflict scan for a small speed-up)."),
            on_changed=lambda _v: self._rebuild_conflicts())

        # Read live by the modlist view on each hover, so persisting the value
        # is enough - no rebuild/refresh needed.
        self._checkbox(
            g, self.tr("Show mod description tooltips"),
            uc.load_show_summary_tooltips, uc.save_show_summary_tooltips,
            help=self.tr("Show a mod's Nexus description as a tooltip when you "
                 "hover over its name in the mod list."))

        self._checkbox(
            g, self.tr("Hide Ko-Fi button"),
            uc.load_hide_kofi_button, uc.save_hide_kofi_button,
            help=self.tr("Hide the Ko-Fi donation button in the status bar."),
            on_changed=lambda _v: self._apply_support_buttons())

        self._checkbox(
            g, self.tr("Hide Endorse button"),
            uc.load_hide_endorse_button, uc.save_hide_endorse_button,
            help=self.tr("Hide the Endorse AMM button in the status bar."),
            on_changed=lambda _v: self._apply_support_buttons())

        self._lang_sync_btn = self._action_row(
            g, self.tr("Sync language files"), self._on_sync_languages)
        self._finish_section(g)

    def _apply_support_buttons(self):
        """Ask the window to re-apply Ko-Fi / Endorse button visibility live."""
        win = self._window
        if win is not None and hasattr(win, "_apply_support_button_visibility"):
            try:
                win._apply_support_button_visibility()
            except Exception:
                pass

    def _build_theme(self, g):
        """Responsive theme gallery. Selections persist and apply immediately."""
        row = self._next_row(g)
        self._theme_gallery_host = QWidget()
        self._theme_gallery_host.setObjectName("ThemeGallery")
        self._theme_gallery = FlowLayout(
            self._theme_gallery_host, margin=0, spacing=12)
        enable_height_for_width(self._theme_gallery_host)
        g.addWidget(self._theme_gallery_host, row, self.COL_LABEL, 1, 2)
        self.refresh_theme_options()

    def refresh_theme_options(self, select_id: str | None = None):
        """Reload built-in/custom palettes and rebuild their preview tiles."""
        try:
            from Utils.themes import load_display_names, load_palettes
            names = load_display_names()
            palettes = load_palettes()
        except Exception:
            names = {"dark": "Dark", "light": "Light"}
            palettes = {}
        current = select_id
        if current is None:
            try:
                current = uc.get_appearance_mode()
            except Exception:
                current = "dark"
        gallery = getattr(self, "_theme_gallery", None)
        if gallery is None:
            return

        while gallery.count():
            item = gallery.takeAt(0)
            widget = item.widget() if item is not None else None
            if widget is not None:
                widget.deleteLater()

        # A malformed custom theme is deliberately absent from load_palettes;
        # do not show a dead choice just because its display name was readable.
        ordered_ids = list(palettes)
        if not ordered_ids:
            # The app itself uses the same dark fallback if discovery fails.
            palettes = {"dark": active_palette()}
            ordered_ids = ["dark"]
            names.setdefault("dark", "Dark")
        selected = (current if current in palettes else
                    "dark" if "dark" in palettes else ordered_ids[0])

        self._theme_buttons: dict[str, _ThemePreviewButton] = {}
        for tid in ordered_ids:
            display = names.get(tid, tid.replace("_", " ").title())
            holder = QWidget()
            holder.setObjectName("ThemeOption")
            holder.setFixedWidth(108)
            holder.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Preferred)
            layout = QVBoxLayout(holder)
            layout.setContentsMargins(2, 2, 2, 2)
            layout.setSpacing(4)
            layout.setAlignment(Qt.AlignTop | Qt.AlignHCenter)

            tile = _ThemePreviewButton(tid, display, palettes[tid], holder)
            tile.setChecked(tid == selected)
            tile.clicked.connect(
                lambda _checked=False, theme_id=tid:
                self._on_theme_changed(theme_id))
            layout.addWidget(tile, 0, Qt.AlignHCenter)

            label = QLabel(display)
            label.setAlignment(Qt.AlignHCenter | Qt.AlignTop)
            label.setWordWrap(True)
            label.setToolTip(display)
            layout.addWidget(label)
            self._theme_buttons[tid] = tile
            gallery.addWidget(holder)

        self._active_theme_id = selected

    def _set_active_theme(self, theme_id: str) -> None:
        self._active_theme_id = theme_id
        for tid, button in getattr(self, "_theme_buttons", {}).items():
            button.setChecked(tid == theme_id)
            button.update()

    def _on_theme_changed(self, tid: str):
        """Persist and immediately apply the selected theme."""
        self._set_active_theme(tid)
        self._safe_save(uc.save_appearance_mode, tid)
        from gui_qt.theme_qt import apply_theme
        app = QApplication.instance()
        if app is not None:
            apply_theme(app)

    def _open_theme_editor(self):
        """Close Settings, then open the full-screen theme editor tab."""
        opener = getattr(self._window, "_open_theme_editor_tab", None)
        if callable(opener):
            self._finish()
            opener()

    def _open_install_name_patterns(self):
        """Close Settings, then open the install-name rules editor tab."""
        opener = getattr(self._window, "_open_install_name_patterns_tab", None)
        if callable(opener):
            self._finish()
            opener()

    def _open_env_vars(self):
        """Close Settings, then open the environment-variable editor tab."""
        opener = getattr(self._window, "_open_env_vars_tab", None)
        if callable(opener):
            self._finish()
            opener()

    def _build_ui_scale(self, g):
        """Add the UI Scale row: an Auto checkbox + a 50–200% slider.

        When Auto is ticked the slider is disabled and the saved value is the
        string 'auto' (resolved to the detected HiDPI scale at load time);
        unticking enables the slider and saves a float multiplier. Either edit
        offers a self-restart so the new QT_SCALE_FACTOR takes effect.
        """
        current = uc.get_ui_scale()          # float, already loaded at startup
        is_auto = uc.load_ui_scale_is_auto()
        pct = int(round(max(0.5, min(3.0, current)) * 100))

        # Percent slider. Persisting + the restart prompt fire only when the
        # user finishes the gesture (sliderReleased / keyboard / click), never
        # on every 1% valueChanged tick while dragging - otherwise the restart
        # overlay would reappear on every intermediate value. The value label
        # still tracks live via valueChanged. Pass a no-op change cb to _slider
        # so it doesn't wire its own per-tick persist.
        self._scale_slider, self._scale_val_lbl = self._slider(
            g, self.tr("UI Scale"), 50, 200, pct, lambda _v: None,
            help=self.tr("Make the whole interface bigger or smaller. "
               "Changes take effect after a restart."))
        self._scale_slider.setSingleStep(5)
        self._scale_slider.setPageStep(10)
        self._scale_val_lbl.setText(f"{pct}%")
        # valueChanged fires on every tick - while dragging (isSliderDown()) it
        # only updates the label; a change that lands with the handle NOT held
        # down (arrow keys, groove click, page step) commits immediately.
        # sliderReleased then commits the final value at the end of a drag.
        self._scale_slider.valueChanged.connect(self._on_scale_value_changed)
        self._scale_slider.sliderReleased.connect(self._commit_ui_scale)
        self._scale_slider.setEnabled(not is_auto)

        # Auto checkbox - sits below the slider; ticking it disables the slider.
        # Placed in the control column (not spanning from the label column) so
        # it reads as a modifier of the UI Scale slider directly above it.
        self._scale_auto_cb = QCheckBox(self.tr("Auto (match display)"))
        self._scale_auto_cb.setChecked(is_auto)
        self._scale_auto_cb.toggled.connect(self._on_ui_scale_auto_toggled)
        g.addWidget(self._scale_auto_cb, self._next_row(g), self.COL_CTRL, 1, 2)

    def _on_ui_scale_auto_toggled(self, on: bool):
        self._scale_slider.setEnabled(not on)
        if on:
            # Enabling auto switches to the detected scale, which differs from
            # whatever manual value is applied - needs a restart to take effect.
            self._safe_save(uc.save_ui_scale, "auto")
            self._prompt_scale_restart()
        else:
            # Disabling auto just re-enables the slider; nothing is persisted
            # until the user actually moves it (which commits + prompts then).
            # Writing the slider's current value here would freeze the
            # auto-detected scale as a manual choice - GH#337: a wrong auto
            # value got cemented that way and later detection fixes never
            # reached the user's ini.
            pass

    def _on_scale_value_changed(self, pct: int):
        """Live label update on every tick. Commit immediately only when the
        handle is NOT being dragged (keyboard/click); a drag commits on release
        via sliderReleased, so we don't prompt on every intermediate value."""
        self._scale_val_lbl.setText(f"{pct}%")
        if not self._scale_slider.isSliderDown():
            self._commit_ui_scale()

    def _commit_ui_scale(self):
        """Persist the current slider value and offer a restart. Called when the
        user finishes changing the slider (release / click / key), so the
        restart prompt appears once per gesture, not per 1% tick."""
        # Only persist when the user is driving the slider (Auto off).
        if getattr(self, "_scale_auto_cb", None) is not None \
                and self._scale_auto_cb.isChecked():
            return
        pct = self._scale_slider.value()
        self._safe_save(uc.save_ui_scale, pct / 100.0)
        self._prompt_scale_restart()

    def _prompt_scale_restart(self):
        self._prompt_restart("scale")

    def _prompt_restart(self, kind: str):
        """Offer a self-restart for startup-only UI scale/language changes."""
        win = self._window
        by_kind = {
            "scale": "_prompt_ui_scale_restart",
            "language": "_prompt_language_restart",
        }
        names = [by_kind.get(kind, "")]
        names += ["_prompt_ui_scale_restart", "_prompt_language_restart"]
        for name in names:
            prompt = getattr(win, name, None)
            if callable(prompt):
                try:
                    prompt()
                    return
                except Exception:
                    pass

    def _populate_language_combo(self):
        """(Re)fill the Language combo from i18n.available_languages(), storing
        each locale code as item-data and preserving the current selection. Owns
        its own save wiring (the generic _combo closure can't repopulate)."""
        from gui_qt.i18n import available_languages
        combo = getattr(self, "_lang_combo", None)
        if combo is None:
            return
        combo.blockSignals(True)
        current = uc.load_language()
        # Disconnect only our own slot - a bare disconnect() would also sever
        # any other connection on the signal.
        prev_slot = getattr(self, "_lang_combo_slot", None)
        if prev_slot is not None:
            combo.currentIndexChanged.disconnect(prev_slot)
            self._lang_combo_slot = None
        combo.clear()
        sel = 0
        for i, (disp, code) in enumerate(available_languages()):
            combo.addItem(disp, userData=code)
            if code == current:
                sel = i
        combo.setCurrentIndex(sel)
        self._lang_combo_slot = (
            lambda i: self._on_language_changed(combo.itemData(i)))
        combo.currentIndexChanged.connect(self._lang_combo_slot)
        combo.blockSignals(False)

    def _on_language_changed(self, code):
        """Persist the chosen language, then offer a restart (same pattern as UI
        scale) so the new translator applies on a fresh launch."""
        self._safe_save(uc.save_language, code)
        self._prompt_restart("language")

    def refresh_language_options(self):
        """Called when a background sync adds new .qm files - refresh the picker
        so newly-downloaded languages appear without reopening Settings."""
        self._populate_language_combo()

    def _on_sync_languages(self):
        """Manually pull the latest translations from the Resources branch. The
        worker fires the window's _languages_synced signal (→ refresh + toast)
        just like the automatic startup sync."""
        try:
            self._window._sync_languages_now()
        except Exception:
            pass

    def _build_archives(self):
        g = self._section(self.tr("Archives"))
        self._checkbox(
            g, self.tr("Clear archive after install"),
            uc.load_clear_archive_after_install,
            uc.save_clear_archive_after_install,
            help=self.tr("Delete a mod's downloaded archive after it is extracted. "
                 "Only applies to archives Amethyst downloaded itself - installs "
                 "from the Install Mod button or the Downloads tab keep their "
                 "archive."))
        self._checkbox(
            g, self.tr("Keep FOMOD archives"),
            uc.load_keep_fomod_archives, uc.save_keep_fomod_archives,
            help=self.tr("Mods installed via a FOMOD installer keep their archive even "
                 "when 'Clear archive after install' is on."))
        self._checkbox(
            g, self.tr("Install new mods disabled"),
            uc.load_install_mods_disabled, uc.save_install_mods_disabled,
            help=self.tr("Newly installed mods start disabled in the modlist instead "
                 "of enabled. Applies to every install path except collection "
                 "installs."))
        self._finish_section(g)

    def _build_downloads(self):
        # Collection settings - all persisted together via save_collection_settings.
        g = self._section(self.tr("Downloads"))
        self._slider(
            g, self.tr("Max concurrent downloads"), 1, uc._MAX_CONCURRENT_CEILING,
            self._cs["max_concurrent"], self._save_max_concurrent)

        # Download speed limit - global cap shared by all download threads.
        lim_sld, lim_lbl = self._slider(
            g, self.tr("Download speed limit"), 0, 250,
            int(uc.load_download_speed_limit()), self._save_speed_limit,
            help=self.tr("Cap the combined download speed of all downloads "
               "(collections, single mods, nxm links) so they don't use the "
               "whole connection. Applies immediately, including to a running "
               "collection install."))
        def _fmt_limit(v, _lbl=lim_lbl):
            _lbl.setText(self.tr("Unlimited") if int(v) == 0 else
                         self.tr("{0} MB/s").format(int(v)))
        lim_sld.valueChanged.connect(_fmt_limit)
        _fmt_limit(lim_sld.value())

        self._checkbox(
            g, self.tr("Download only (don't install)"),
            uc.load_download_only, uc.save_download_only,
            help=self.tr("Downloads are saved to the cache but not installed. Applies "
                 "to nxm:// links, the Nexus browser, Change Version, collection "
                 "installs, requirement downloads and update/reinstall redownloads "
                 "- their Install buttons become Download. Install them yourself "
                 "from the Downloads tab or the Install Mod button."),
            on_changed=self._on_download_only_changed)

        # Manage Caches action - a footer button like every other action,
        # rather than a "Caches" label paired with a button as if it were a
        # setting with a value.
        self._cache_btn = self._action_row(
            g, self.tr("Manage Caches…"), self._on_manage_caches)
        self._finish_section(g)

    def _build_extraction(self):
        # Extraction resource limits - apply to every install (single mods,
        # Downloads tab and collections), not just collection installs.
        g = self._section(self.tr("Extraction"))
        self._slider(
            g, self.tr("Max extractions"), 1, uc._MAX_EXTRACT_WORKERS_CEILING,
            self._cs["max_extract_workers"], self._save_max_extract,
            help=self.tr("Extractions are gated by available memory; the effective number "
               "may be lower than set."))

        import os as _os
        ext = uc.load_extraction_settings()
        thr_sld, thr_lbl = self._slider(
            g, self.tr("Extraction CPU threads"), 0, _os.cpu_count() or 8,
            int(ext.get("cpu_threads", 0)),
            lambda v: self._safe_save(uc.save_extraction_cpu_threads, v),
            help=self.tr("CPU threads each extraction may use. 'All' is fastest; a "
               "lower value keeps the system responsive while large archives "
               "extract."))
        def _fmt_threads(v, _lbl=thr_lbl):
            _lbl.setText(self.tr("All") if int(v) == 0 else str(v))
        thr_sld.valueChanged.connect(_fmt_threads)
        _fmt_threads(thr_sld.value())
        self._checkbox(
            g, self.tr("Low priority extractions"),
            lambda: bool(uc.load_extraction_settings().get("low_priority", False)),
            uc.save_extraction_low_priority,
            help=self.tr("Run extractions at low CPU and disk priority so they yield "
                 "to other applications instead of slowing them down. Extraction "
                 "speed is unaffected while the system is otherwise idle."))
        self._finish_section(g)

    def _build_general(self):
        g = self._section(self.tr("General"))
        self._checkbox(
            g, self.tr("Normalise folder casing"),
            uc.load_normalize_folder_case, uc.save_normalize_folder_case,
            help=self.tr("Unify folder names to a single casing across mods. Disable on "
                 "case-insensitive filesystems."))
        self._checkbox(
            g, self.tr("Rename mod after install"),
            uc.load_rename_mod_after_install, uc.save_rename_mod_after_install,
            help=self.tr("Show a rename prompt after installing a mod."))
        # Custom install-name rules - a full editor (opened as its own tab)
        # rather than a single control, so it goes in the section footer
        # instead of interrupting the run of checkboxes.
        self._action_row(
            g, self.tr("Edit custom install-name rules…"),
            self._open_install_name_patterns,
            help=self.tr(
                "Add your own regex search/replace rules to clean up mod names "
                "on install - useful when a download site changes its filename "
                "format."))
        self._checkbox(
            g, self.tr("Restore on close"),
            uc.load_restore_on_close, uc.save_restore_on_close,
            help=self.tr("Restore all deployed games to vanilla when the app is closed."))
        self._checkbox(
            g, self.tr("Use pre-release versions"),
            uc.load_allow_prerelease, uc.save_allow_prerelease,
            help=self.tr("Also offer beta and release-candidate app builds when checking "
                 "for updates."),
            on_changed=self._on_prerelease_toggle)
        self._checkbox(
            g, self.tr("Notify about new versions on startup"),
            uc.load_update_notifications, uc.save_update_notifications,
            help=self.tr("Show a notification when a new version of Amethyst "
                 "is available. Turning this off only mutes the notification "
                 "- you can still update via your package manager or by "
                 "toggling the pre-release setting."))
        self._action_row(
            g, self.tr("Reset dismissed prompts…"),
            self._on_reset_dismissed_notices,
            help=self.tr(
                "Bring back every notice you hid by ticking \"Don't show this "
                "again\" - the launcher handoff notice, the Windows filesystem "
                "warning and the rest."))

        self._maybe_add_flatpak_enroll(g)
        self._finish_section(g)

    def _on_reset_dismissed_notices(self):
        """Confirm, then re-arm every "Don't show this again" notice."""
        from gui_qt.confirm_overlay import ConfirmOverlay
        hidden = uc.count_dismissed_notices()
        if not hidden:
            ConfirmOverlay.show_message(
                self._window, self.tr("Nothing to reset"),
                self.tr("No prompts are currently hidden."))
            return

        def _go(ok: bool):
            if not ok:
                return
            n = uc.reset_dismissed_notices()
            if n == 1:
                done = self.tr("{0} hidden prompt will show again.").format(n)
            else:
                done = self.tr("{0} hidden prompts will show again.").format(n)
            ConfirmOverlay.show_message(
                self._window, self.tr("Prompts reset"), done)

        if hidden == 1:
            body = self.tr("{0} prompt is hidden. It will start showing "
                           "again.").format(hidden)
        else:
            body = self.tr("{0} prompts are hidden. They will start showing "
                           "again.").format(hidden)
        ConfirmOverlay.show_over(
            self._window, self.tr("Reset dismissed prompts?"), body, _go,
            confirm_label=self.tr("Reset"),
            cancel_label=self.tr("Cancel"),
            danger=False,
        )

    def _maybe_add_flatpak_enroll(self, g):
        """Offer a one-time 'Enable automatic updates' button to flatpak users
        who installed from a bundle and aren't yet tracking our remote.

        Adding the remote hands updates to the OS (native `flatpak update`,
        GNOME Software / Discover, delta downloads). Once enrolled, the button
        is hidden. No-op outside the flatpak or when already remote-tracked.
        """
        from Utils.version_check import (
            is_flatpak, flatpak_installed_from_remote,
        )
        if not is_flatpak() or flatpak_installed_from_remote():
            return
        self._action_row(
            g, self.tr("Enable automatic updates…"),
            self._on_enroll_flatpak_remote,
            help=self.tr(
                "Switch this Flatpak to the Amethyst update remote so future "
                "updates arrive automatically through your package manager "
                "(GNOME Software / Discover) with smaller downloads. This "
                "reinstalls the app once from the remote and relaunches it."))

    def _on_enroll_flatpak_remote(self):
        """Confirm, then add the remote + reinstall-from-remote (relaunches)."""
        from gui_qt.confirm_overlay import ConfirmOverlay
        from Utils.version_check import enroll_flatpak_remote
        from Utils.ui_config import load_allow_prerelease

        def _go(ok: bool):
            if not ok:
                return
            allow_pre = load_allow_prerelease()
            status = enroll_flatpak_remote(allow_prerelease=allow_pre)
            if status == "launched":
                win = self._window
                if win is not None:
                    win.close()  # the detached child relaunches from the remote
            elif status == "no-branch":
                channel = self.tr("beta") if allow_pre else self.tr("stable")
                ConfirmOverlay.show_message(
                    self._window,
                    self.tr("Channel not available"),
                    self.tr("The {0} channel isn't published on the update "
                            "remote yet. Try again after the next {0} "
                            "release (or change the pre-release setting)."
                            ).format(channel))
            else:
                ConfirmOverlay.show_message(
                    self._window,
                    self.tr("Could not reach Flatpak"),
                    self.tr("The host Flatpak service couldn't be reached. "
                            "You can add the remote manually:\n\n"
                            "flatpak remote-add --user amethyst "
                            "https://chrisdkn.github.io/Amethyst-Mod-Manager/"
                            "amethyst.flatpakrepo"))
        ConfirmOverlay.show_over(
            self._window,
            self.tr("Enable automatic updates?"),
            self.tr("Amethyst will add its update remote and reinstall itself "
                    "from it once, then relaunch. Future updates then arrive "
                    "automatically through your package manager."),
            _go,
            confirm_label=self.tr("Enable"),
            cancel_label=self.tr("Cancel"),
            danger=False,
        )

    def _build_paths(self):
        g = self._section(self.tr("Paths"))
        from Utils.config_paths import get_config_dir, get_default_staging_root
        base = get_config_dir()
        self._path_row(
            g, self.tr("Default Mod Staging Folder"),
            uc.load_default_staging_path, uc.save_default_staging_path,
            help=self.tr("When set, games added after this point stage mods here. "
                 "Blank = default ({0}).").format(
                     get_default_staging_root() / self.tr("<game name>")))
        self._path_row(
            g, self.tr("Download Cache Folder"),
            uc.load_download_cache_path, uc.save_download_cache_path,
            help=self.tr("Where downloaded mod archives are stored. "
                 "Blank = default ({0}).").format(base / 'download_cache'))
        self._path_row(
            g, self.tr("Heroic Config Location"),
            uc.load_heroic_config_path, uc.save_heroic_config_path,
            help=self.tr("Folder containing Heroic's config.json. Blank = auto-detect "
                 "(Flatpak and native locations)."))
        self._path_row(
            g, self.tr("Lutris Data Location"),
            uc.load_lutris_data_path, uc.save_lutris_data_path,
            help=self.tr("Folder containing Lutris's pga.db. Blank = auto-detect "
                 "(Flatpak and native locations)."))
        self._file_row(
            g, self.tr("Lutris AppImage"),
            uc.load_lutris_appimage_path, uc.save_lutris_appimage_path,
            filters=[("AppImage", ["*.AppImage", "*.appimage"]), ("All files", ["*"])],
            help=self.tr("Path to the Lutris AppImage, so Play can launch it "
                 "directly. Only needed for AppImage installs - leave blank for "
                 "Flatpak or native Lutris."))
        self._path_row(
            g, self.tr("Faugus Data Location"),
            uc.load_faugus_data_path, uc.save_faugus_data_path,
            help=self.tr("Folder containing Faugus Launcher's games.json. "
                 "Blank = auto-detect (Flatpak and native locations)."))
        self._file_row(
            g, self.tr("Faugus AppImage"),
            uc.load_faugus_appimage_path, uc.save_faugus_appimage_path,
            filters=[("AppImage", ["*.AppImage", "*.appimage"]), ("All files", ["*"])],
            help=self.tr("Path to the Faugus Launcher AppImage, so Play can "
                 "launch it directly. Only needed for AppImage installs - leave "
                 "blank for Flatpak or native Faugus."))
        self._path_row(
            g, self.tr("Steam libraryfolders.vdf"),
            uc.load_steam_libraries_vdf_path, uc.save_steam_libraries_vdf_path,
            help=self.tr("Path to libraryfolders.vdf (or its folder). Blank = auto-detect "
                 "(standard, Flatpak and Snap locations)."))
        self._finish_section(g)

    def _build_advanced(self):
        g = self._section(self.tr("Advanced"))
        # Summarise what's already set so the section isn't a blind door.
        try:
            active = [e["name"] for e in uc.load_app_env_vars() if e.get("enabled")]
        except Exception:
            active = []
        summary = QLabel(
            self.tr("{0} set: {1}").format(len(active), ", ".join(active))
            if active else self.tr("None set"))
        summary.setObjectName("Help")
        summary.setWordWrap(True)
        self._action_row(
            g, self.tr("Edit environment variables…"), self._open_env_vars,
            help=self.tr(
                "Set environment variables that Amethyst applies to itself "
                "every time it starts - kill switches, diagnostics and "
                "graphics options that otherwise need a terminal launch. Pick "
                "from the supported list or add your own."),
            extra=summary)
        self._finish_section(g)

    def _build_system_info(self):
        """Read-only environment facts + a Copy button, for bug reports."""
        g = self._section(self.tr("System Information"))
        from Utils import system_info

        try:
            pairs = system_info.collect()
        except Exception:
            pairs = []

        # Labels are translated here as literals - lupdate cannot extract a
        # `tr(variable)`, so the keys from system_info map to real tr() calls.
        names = {
            "App version": self.tr("App version"),
            "OS": self.tr("OS"),
            "Distribution": self.tr("Distribution"),
            "Kernel": self.tr("Kernel"),
            "Python": self.tr("Python"),
            "Qt": self.tr("Qt"),
            "Run mode": self.tr("Run mode"),
            "Package": self.tr("Package"),
            "Desktop": self.tr("Desktop"),
            "Session": self.tr("Session"),
            "OpenGL": self.tr("OpenGL"),
            "Env overrides": self.tr("Env overrides"),
        }

        # Tighter than the option sections: these are dense read-only pairs,
        # not controls that need room to be clicked.
        g.setVerticalSpacing(4)
        for label, value in pairs:
            row = self._next_row(g)
            g.addWidget(QLabel(names.get(label, label)), row, self.COL_LABEL)
            edit = QLineEdit(str(value))
            edit.setReadOnly(True)
            # Selectable so a single value can be copied without the whole block.
            edit.setCursorPosition(0)
            edit.setToolTip(str(value))
            g.addWidget(edit, row, self.COL_CTRL)

        self._action_row(
            g, self.tr("Copy to clipboard"), self._copy_system_info)
        self._finish_section(g)

    def _copy_system_info(self):
        from PySide6.QtWidgets import QApplication
        from Utils import system_info
        try:
            QApplication.clipboard().setText("\n".join(system_info.log_lines()))
        except Exception:
            return
        self._notify(self.tr("System information copied."), "info")

    # ---- collection setting handlers (all persist the whole group) --------
    def _persist_collection(self):
        self._safe_save(
            uc.save_collection_settings,
            self._cs["max_concurrent"],
            self._cs["check_download_locations"],
            self._cs["clear_archive_after_install"],
            self._cs["max_extract_workers"])

    def _save_max_concurrent(self, value: int):
        self._cs["max_concurrent"] = int(value)
        self._persist_collection()

    def _save_max_extract(self, value: int):
        self._cs["max_extract_workers"] = int(value)
        self._persist_collection()

    def _save_speed_limit(self, value: int):
        # Apply to in-flight downloads immediately, then persist.
        from Utils import bandwidth_limit
        bandwidth_limit.set_limit_mbps(float(value))
        self._safe_save(uc.save_download_speed_limit, float(value))

    def _on_download_only_changed(self, _value):
        """Relabel Install/Download on any tab open alongside Settings."""
        # _checkbox persists before calling here, so the views re-read the new value.
        w = self._window
        for attr in ("_nexus_view", "_thunderstore_view", "_change_version_view",
                     "_missing_reqs_view"):
            view = getattr(w, attr, None)
            if view is None:
                continue
            try:
                view.refresh_install_labels()
            except Exception:
                pass
        try:
            w._refresh_open_collection_buttons()
        except Exception:
            pass


    # ---- path browse / clear ----------------------------------------------
    def _browse_into(self, edit: QLineEdit, save_fn, title: str):
        from Utils.portal_filechooser import pick_folder
        pick_folder(f"Select {title}",
                    lambda path: self._folder_picked.emit((edit, save_fn, path)))

    def _browse_file_into(self, edit: QLineEdit, save_fn, title: str,
                          filters=None):
        # Reuse the folder-picked signal/slot - the payload shape is identical
        # (edit, save_fn, path); pick_file just returns a file Path.
        from Utils.portal_filechooser import pick_file
        pick_file(f"Select {title}",
                  lambda path: self._folder_picked.emit((edit, save_fn, path)),
                  filters=filters)

    def _on_folder_picked(self, payload):
        edit, save_fn, path = payload
        if path:
            edit.setText(str(path))
            self._safe_save(save_fn, str(path))

    def _clear_path(self, edit: QLineEdit, save_fn):
        edit.clear()
        self._safe_save(save_fn, "")

    # ---- Manage Caches ----------------------------------------------------
    def _on_manage_caches(self):
        """Open the borderless per-game cache browser overlay (Tk parity)."""
        from gui_qt.cache_manager_overlay import CacheManagerOverlay
        active = getattr(getattr(self._window, "_gs", None), "game_name", "") or ""
        CacheManagerOverlay.show_over(
            self._window, active_game_name=active)

    def _on_prerelease_toggle(self, value: bool):
        """Re-run the app update check immediately (Tk parity).

        When *unticking* (opting out), pass force_downgrade_prompt=True so the
        user is offered a switch to the latest stable even if it's older than
        the pre-release they're currently running. When *ticking*, no force -
        the normal upgrade check already handles "is there a newer build?".
        """
        check = getattr(self._window, "_check_for_app_update", None)
        if callable(check):
            check(force_downgrade_prompt=not value, force_fresh=True)

    # ---- helpers ----------------------------------------------------------
    def _rebuild_conflicts(self):
        """Ask the window to rebuild conflicts so a setting that affects them
        (e.g. Hide BSA conflicts) applies live without a manual refresh."""
        win = self._window
        if win is not None and hasattr(win, "_rebuild_conflicts_async"):
            try:
                win._rebuild_conflicts_async()
            except Exception:
                pass

    def _safe_save(self, save_fn, *args):
        try:
            save_fn(*args)
        except Exception as exc:
            self._notify(self.tr("Failed to save setting: {0}").format(exc), "warning")

    def _notify(self, text: str, state: str = "info"):
        win = self._window
        if win is not None and hasattr(win, "_notify"):
            try:
                win._notify(text, state)
                return
            except Exception:
                pass
        print(f"[settings] {text}")
