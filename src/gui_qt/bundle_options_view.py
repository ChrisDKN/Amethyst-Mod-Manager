"""Bundle Options view - pick which options of a RE/Fluffy bundle mod are active.

Opens as a plugins-panel-scoped tab (covers the whole plugins panel), the Qt port
of Tk's gui/dialogs.py BundleOptionsPanel. A bundle installs as ONE mod whose
meta.ini carries a [Bundle] spec; the original option folders live untouched under
``<mod>/.mm_bundle/``. This view edits a deep copy of the spec's selection/order and,
on Save, hands the new spec back so the app can re-materialise + rebuild the filemap.

Select-one groups render as exclusive radios; independent ("Optional - any") groups
as checkboxes with ▲/▼ reorder buttons. When two selected options write the same
file, the one LOWER in the list wins (applied last) - reorder to change that. An
option whose files are entirely shadowed by another selected option is marked
"(overridden)" in red. Checking an option auto-turns-off any other selected option
with an identical file set ("your click wins"). Hovering an option shows its
screenshot in the inline preview pane. All the underlying logic is GUI-free in
Utils.re_bundle.
"""

from __future__ import annotations

import copy

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QRadioButton,
    QCheckBox, QButtonGroup, QScrollArea, QSizePolicy, QSplitter,
)

from gui_qt.theme_qt import (
    active_palette, bind_theme_icon, _c, danger_close_button,
)
from gui_qt.image_preview import _load_qimage
from PySide6.QtGui import QPixmap

from Utils.re_bundle import (
    option_deployable_rels, option_image, option_description,
)

_HELP_TEXT = (
    "Choose which options are active. “Select one” groups allow a single "
    "choice; optional add-ons can be combined.\n"
    "When optional add-ons overlap, the one lower in the list wins - use "
    "▲/▼ to reorder.  Checking an add-on turns off any lower one it fully "
    "replaces, so your choice wins."
)


class _PreviewPane(QWidget):
    """Inline right-side pane showing the hovered/selected option's screenshot."""

    def __init__(self, p):
        super().__init__()
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)
        header = QLabel(self.tr("Preview"))
        header.setStyleSheet(
            f"background:{_c(p,'BG_HEADER')}; color:{_c(p,'TEXT_DIM')};"
            " padding:6px 10px; font-weight:600;")
        header.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        v.addWidget(header)
        self._canvas = QLabel()
        self._canvas.setAlignment(Qt.AlignCenter)
        self._canvas.setMinimumWidth(240)
        self._canvas.setStyleSheet(f"background:{_c(p,'BG_DEEP')};")
        self._canvas.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        v.addWidget(self._canvas, 1)
        self._pm: QPixmap | None = None
        self._empty()

    def _empty(self):
        self._pm = None
        self._canvas.setText(self.tr("No preview"))

    def set_image_path(self, path):
        if path is None:
            self._empty()
            return
        qi = _load_qimage(path)
        if qi is None:
            self._empty()
            return
        self._pm = QPixmap.fromImage(qi)
        self._rescale()

    def _rescale(self):
        if self._pm is None:
            return
        area = self._canvas.size()
        scaled = self._pm.scaled(
            area, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        self._canvas.setPixmap(scaled)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._rescale()


class BundleOptionsView(QWidget):
    """Scoped-tab body for editing a bundle's active options + order."""

    def __init__(self, mod_name, spec, lib_dir, on_save, on_close, log_fn=None):
        super().__init__()
        self._mod_name = mod_name
        self._spec = copy.deepcopy(spec)
        self._lib_dir = lib_dir
        self._on_save = on_save or (lambda _s: None)
        self._on_close = on_close or (lambda: None)
        self._log = log_fn or (lambda _m: None)

        self.setObjectName("BundleOptionsView")

        # Per-option deployable file sets (lowercased mod-root rel paths), used to
        # flag options that write the same file as another selected option. Built
        # once from the bundle library; empty when no lib_dir is available.
        self._opt_files: dict[str, set[str]] = {}
        if self._lib_dir is not None:
            for g in self._spec.groups:
                for o in g.options:
                    if o.is_label:
                        continue
                    try:
                        self._opt_files[o.folder] = option_deployable_rels(
                            self._lib_dir, o.folder)
                    except Exception:
                        self._opt_files[o.folder] = set()

        # folder -> {"marker": QLabel, "opt": BundleOption, "checkbox"/"radio": w}
        self._opt_widgets: dict[str, dict] = {}

        self._build()

    # ---- layout -----------------------------------------------------------
    def _build(self):
        p = active_palette()
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)

        # Header bar: title + ✕ Close (same red close button as ChangeVersionView).
        bar = QWidget(); bar.setObjectName("HeaderBar")
        hb = QHBoxLayout(bar); hb.setContentsMargins(12, 8, 8, 8); hb.setSpacing(8)
        title = QLabel(self.tr("Bundle Options - {0}").format(self._mod_name))
        title.setStyleSheet(f"color:{_c(p,'TEXT_MAIN')}; font-weight:600;")
        hb.addWidget(title)
        hb.addStretch(1)
        close = danger_close_button(pal=p)
        close.clicked.connect(lambda: self._on_close())
        hb.addWidget(close)
        v.addWidget(bar)

        # Help text.
        help_lbl = QLabel(_HELP_TEXT)
        help_lbl.setWordWrap(True)
        help_lbl.setStyleSheet(f"color:{_c(p,'TEXT_DIM')}; padding:10px 16px 6px 16px;")
        v.addWidget(help_lbl)

        # Body: option list (left) + inline preview (right), split by a draggable
        # divider so the user can resize the two panes.
        split = QSplitter(Qt.Horizontal)
        split.setChildrenCollapsible(False)
        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setStyleSheet(
            f"QScrollArea{{background:{_c(p,'BG_PANEL')}; border:1px solid "
            f"{_c(p,'BORDER')}; border-radius:6px;}}")
        self._list_host = QWidget()
        self._list_host.setStyleSheet(f"background:{_c(p,'BG_PANEL')};")
        self._scroll.setWidget(self._list_host)
        split.addWidget(self._scroll)
        self._preview = _PreviewPane(p)
        split.addWidget(self._preview)
        split.setStretchFactor(0, 3)
        split.setStretchFactor(1, 2)
        split.setSizes([600, 400])
        body = QHBoxLayout(); body.setContentsMargins(12, 0, 12, 4); body.setSpacing(0)
        body.addWidget(split)
        v.addLayout(body, 1)

        # Footer: Save / Cancel.
        footer = QWidget(); footer.setObjectName("HeaderBar")
        fb = QHBoxLayout(footer); fb.setContentsMargins(12, 8, 12, 8); fb.setSpacing(8)
        fb.addStretch(1)
        cancel = QPushButton(self.tr("Cancel")); cancel.setCursor(Qt.PointingHandCursor)
        cancel.clicked.connect(lambda: self._on_close())
        fb.addWidget(cancel)
        save = QPushButton(self.tr("Save")); save.setCursor(Qt.PointingHandCursor)
        save.setStyleSheet(
            f"QPushButton{{background:{_c(p,'ACCENT')}; color:"
            f"{_c(p,'TEXT_ON_ACCENT')}; border:none; padding:5px 16px;"
            " border-radius:4px; font-weight:600;}")
        save.clicked.connect(self._on_save_clicked)
        fb.addWidget(save)
        v.addWidget(footer)

        self._build_rows()

        # Show the first selected option's image as the initial preview.
        first = next((o for g in self._spec.groups for o in g.options
                      if o.selected and not o.is_label), None)
        if first is not None:
            self._preview_option(first.folder)

    def _build_rows(self):
        """(Re)build the option rows from the current spec. Called on init and
        after any reorder so the displayed order matches ``group.options``."""
        p = active_palette()
        # Replace the list host's layout wholesale (clears old rows + widgets).
        old = self._list_host.layout()
        if old is not None:
            QWidget().setLayout(old)   # reparent old layout away → drops its widgets
        self._opt_widgets = {}
        self._button_groups = []       # keep QButtonGroup refs alive

        col = QVBoxLayout(self._list_host)
        col.setContentsMargins(10, 8, 10, 12)
        col.setSpacing(2)

        for group in self._spec.groups:
            gl = QLabel(group.name)
            gl.setStyleSheet(f"color:{_c(p,'TEXT_MAIN')}; font-weight:600; margin-top:10px;")
            col.addWidget(gl)
            sub = QLabel(self.tr("Select one") if group.select_one else self.tr("Optional - any"))
            sub.setStyleSheet(f"color:{_c(p,'TEXT_DIM')};")
            col.addWidget(sub)

            if group.select_one:
                self._build_select_one(group, col)
            else:
                self._build_independent(group, col)

        col.addStretch(1)
        self._recompute_conflicts()

    def _build_select_one(self, group, col):
        p = active_palette()
        bgroup = QButtonGroup(self)
        bgroup.setExclusive(True)
        self._button_groups.append(bgroup)
        for opt in group.options:
            if opt.is_label:
                col.addWidget(self._label_row(opt.label, p))
                continue
            row = QWidget()
            h = QHBoxLayout(row); h.setContentsMargins(14, 1, 0, 1); h.setSpacing(6)
            rb = QRadioButton(opt.label)
            rb.setChecked(opt.selected)
            rb.setStyleSheet(f"color:{_c(p,'TEXT_MAIN')};")
            self._attach_tooltip(rb, opt)
            bgroup.addButton(rb)
            rb.toggled.connect(
                lambda checked, g=group, o=opt: self._on_radio(checked, g, o))
            h.addWidget(rb)
            marker = self._marker_label()
            h.addWidget(marker)
            h.addStretch(1)
            self._install_hover(row, opt.folder)
            self._opt_widgets[opt.folder] = {"marker": marker, "opt": opt}
            col.addWidget(row)

    def _build_independent(self, group, col):
        p = active_palette()
        n = len(group.options)
        for oi, opt in enumerate(group.options):
            if opt.is_label:
                col.addWidget(self._label_row(opt.label, p))
                continue
            row = QWidget()
            h = QHBoxLayout(row); h.setContentsMargins(10, 1, 0, 1); h.setSpacing(6)
            cb = QCheckBox(opt.label)
            cb.setChecked(opt.selected)
            cb.setStyleSheet(f"color:{_c(p,'TEXT_MAIN')};")
            self._attach_tooltip(cb, opt)
            cb.toggled.connect(
                lambda checked, o=opt: self._on_checkbox(checked, o))
            h.addWidget(cb)
            marker = self._marker_label()
            h.addWidget(marker)
            h.addStretch(1)
            up = QPushButton(); up.setFixedSize(26, 24)
            bind_theme_icon(up, "arrow.png", 12, "TEXT_MAIN", degrees=180)
            up.setToolTip(self.tr("Move up"))
            up.setEnabled(oi > 0)
            up.clicked.connect(lambda _=False, g=group, i=oi: self._move(g, i, -1))
            h.addWidget(up)
            down = QPushButton(); down.setFixedSize(26, 24)
            bind_theme_icon(down, "arrow.png", 12, "TEXT_MAIN", degrees=0)
            down.setToolTip(self.tr("Move down"))
            down.setEnabled(oi < n - 1)
            down.clicked.connect(lambda _=False, g=group, i=oi: self._move(g, i, 1))
            h.addWidget(down)
            self._install_hover(row, opt.folder)
            self._opt_widgets[opt.folder] = {"marker": marker, "opt": opt, "checkbox": cb}
            col.addWidget(row)

    def _label_row(self, text, p):
        lbl = QLabel(text)
        lbl.setStyleSheet(f"color:{_c(p,'TEXT_DIM')}; margin-left:14px;")
        return lbl

    def _marker_label(self):
        m = QLabel("")
        m.setStyleSheet(f"color:{_c(active_palette(), 'BG_RED_TEXT')};")
        return m

    def _attach_tooltip(self, widget, opt):
        desc = ""
        if self._lib_dir is not None:
            try:
                desc = option_description(self._lib_dir, opt.folder)
            except Exception:
                desc = ""
        widget.setToolTip(f"{opt.label}\n\n{desc}" if desc else opt.label)

    def _install_hover(self, widget, folder):
        """Show the option's screenshot while the pointer is over *widget*."""
        widget.setAttribute(Qt.WA_Hover, True)
        orig = widget.enterEvent

        def enter(event, f=folder, _o=orig):
            self._preview_option(f)
            _o(event)
        widget.enterEvent = enter

    # ---- behaviour --------------------------------------------------------
    def _on_radio(self, checked, group, opt):
        if not checked:
            return   # the newly-off button; the newly-on one fires too
        for o in group.options:
            o.selected = (o is opt) and not o.is_label
        self._recompute_conflicts()

    def _on_checkbox(self, checked, opt):
        opt.selected = bool(checked)
        if opt.selected:
            # "Your click wins": turn off any other selected independent option
            # that writes exactly the same files as the one just checked.
            self._promote_option(opt.folder)
        self._recompute_conflicts()

    def _ordered_selected_folders(self) -> list[str]:
        """Selected option folders in deploy apply order, mirroring
        re_bundle._ordered_selected_folders: select-one groups first (declared
        order), independent groups last, display order within a group (lower =
        applied later = wins)."""
        select_one: list[str] = []
        independent: list[str] = []
        for g in self._spec.groups:
            bucket = select_one if g.select_one else independent
            for o in g.options:
                if o.selected and not o.is_label:
                    bucket.append(o.folder)
        return select_one + independent

    def _promote_option(self, folder: str) -> None:
        """Make the just-checked *folder* win: turn off any other selected
        independent option whose deployable file set is IDENTICAL to *folder*'s
        (a true alternative - the two write exactly the same files). Subset/
        superset pairs are left alone; _recompute_conflicts marks whatever ends
        up fully shadowed."""
        files = self._opt_files.get(folder, set())
        if not files:
            return
        for other, w in self._opt_widgets.items():
            if other == folder or "checkbox" not in w:
                continue   # self, or a radio (can't empty its group)
            o = w["opt"]
            if not o.selected or o.is_label:
                continue
            if self._opt_files.get(other, set()) == files:
                o.selected = False
                cb = w["checkbox"]
                cb.blockSignals(True)
                cb.setChecked(False)
                cb.blockSignals(False)

    def _recompute_conflicts(self):
        """Flag every selected option whose files are entirely overridden by
        another selected option, mirroring deploy order. The LAST selected option
        to write a file wins it; an option none of whose files survive is marked
        '(overridden)' in red."""
        if not self._opt_widgets:
            return
        ordered = self._ordered_selected_folders()
        winners: dict[str, str] = {}
        for folder in ordered:
            for rel in self._opt_files.get(folder, ()):
                winners[rel] = folder
        selected = set(ordered)
        for folder, w in self._opt_widgets.items():
            files = self._opt_files.get(folder, set())
            shadowed = bool(
                folder in selected and files
                and not any(winners.get(rel) == folder for rel in files))
            w["marker"].setText(self.tr("(overridden)") if shadowed else "")

    def _move(self, group, idx: int, delta: int):
        """Swap option *idx* with its neighbour and rebuild the rows."""
        j = idx + delta
        if 0 <= j < len(group.options):
            group.options[idx], group.options[j] = \
                group.options[j], group.options[idx]
            self._build_rows()

    # ---- preview ----------------------------------------------------------
    def _preview_option(self, folder: str) -> None:
        if self._lib_dir is None:
            return
        try:
            img = option_image(self._lib_dir, folder)
        except Exception:
            img = None
        self._preview.set_image_path(img)

    # ---- save -------------------------------------------------------------
    def _on_save_clicked(self):
        # Selection + order already live in self._spec (written on each interaction).
        self._on_save(self._spec)
