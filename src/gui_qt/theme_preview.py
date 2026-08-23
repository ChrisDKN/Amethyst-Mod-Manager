"""Live preview panel for the theme editor.

A self-contained mock of the app's themeable elements, shown to the right of
the colour swatches. ``refresh(pal)`` restyles ONLY this subtree - the working
palette is rendered via ``build_qss``/``build_qpalette`` set on the preview
root (Qt's nearest-ancestor stylesheet wins over the app-wide one), plus a
list of registered updaters for elements the real app paints manually
(delegate brushes, inline-styled banner rows, palette-driven buttons). The
same working palette is also applied temporarily across the open application.

Popup windows such as combo lists and menus follow the same app-wide temporary
palette, while the embedded panel continues to make the full set of roles easy
to inspect inside the editor.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable

from PySide6.QtCore import QEvent, Qt, Signal
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QScrollArea, QFrame,
    QLabel, QComboBox, QLineEdit, QPushButton, QCheckBox, QRadioButton,
    QTabBar, QTreeWidget, QTreeWidgetItem, QListWidget, QProgressBar,
)

from gui_qt.theme_qt import (
    build_qss, build_qpalette, button_qss, contrast_text, qc, qc_contrast, _c,
)
from gui_qt import theme_editor_groups as teg
from gui_qt.icons import icon as _icon
from gui_qt.wheel_guard import no_wheel

# src/icons/ - same directory gui_qt.icons loads from (gui_qt/ is a sibling).
_ICONS_DIR = Path(__file__).resolve().parent.parent / "icons"


# (base fill key, hover key, label) - one sample button per family. Hover is
# interactive: button_qss emits a real :hover rule from the working palette.
_BUTTON_FAMILIES = (
    ("BTN_DANGER", "BTN_DANGER_HOV", "Danger"),
    ("BTN_SUCCESS", "BTN_SUCCESS_HOV", "Success"),
    ("BTN_WARN", "BTN_WARN_HOV", "Warning"),
    ("BTN_INFO", "BTN_INFO_HOV", "Info"),
    ("BTN_NEUTRAL", "BTN_NEUTRAL_HOV", "Neutral"),
    ("BTN_GREY", "BTN_GREY_HOV", "Grey"),
    ("BTN_PURPLE", "BTN_PURPLE_HOV", "Purple"),
)

_TEXT_SAMPLES = (
    ("TEXT_MAIN", "Primary text"),
    ("TEXT_DIM", "Dimmed text"),
    ("TEXT_FAINT", "Faint text"),
    ("TEXT_OK", "Success text"),
    ("TEXT_ERR", "Error text"),
    ("TEXT_WARN", "Warning text"),
    ("TEXT_OK_BRIGHT", "Success (bright)"),
    ("TEXT_ERR_BRIGHT", "Error (bright)"),
    ("TEXT_WARN_BRIGHT", "Warning (bright)"),
    ("LINK_BLUE", "Hyperlink"),
    ("TEXT_SEP", "Separator text"),
)

_TONES = ("TONE_GREEN", "TONE_RED", "TONE_BLUE", "TONE_CYAN",
          "TONE_BLUE_SOFT")

_STATUS_PILLS = (
    ("STATUS_BADGE_RED", "3 errors"),
    ("STATUS_QUEUED", "Queued"),
)

# Icons the app recolours from the palette, with the role each one follows.
# Mirrors the real tint sites: DROPDOWN_ARROW for the expand/collapse chevrons
# (theme_qt QSS, collapsible_section, the data/text/mod-files delegates),
# TEXT_MAIN for the toolbar and modlist mono flags (_MONO_FLAG_ICONS), and
# CHECK_FILL for the checkbox tick. Rendered here at the size the app uses.
_TINTED_ICONS = (
    ("arrow.png", "DROPDOWN_ARROW", "Dropdown / collapse arrow"),
    ("right.png", "DROPDOWN_ARROW", "Expand arrow"),
    ("settings.png", "TEXT_MAIN", "Settings"),
    ("notification.png", "TEXT_MAIN", "Notifications"),
    ("eye1_white.png", "TEXT_MAIN", "Filter / visibility"),
    ("eye2_white.png", "TEXT_MAIN", "Modified mod files flag"),
    ("root.png", "TEXT_MAIN", "Root folder flag"),
    ("bundle_settings.png", "TEXT_MAIN", "Bundle settings flag"),
    ("close_white.png", "TEXT_DIM", "Close / clear"),
    ("check_white.png", "CHECK_FILL", "Checkbox tick"),
    ("proton.png", "TEXT_MAIN", "Proton menu"),
)

# Fixed artwork - shown so the theme's surfaces can be checked against the
# icons that keep their own colours. AmethystBanner/Logo are the app branding
# rather than UI icons, and ui.png / title-bar.png are screenshots, so they are
# all excluded. The remainder is discovered from src/icons/ at build time.
_UNTINTED_EXCLUDE = frozenset({
    "AmethystBanner.png", "Logo.png", "ui.png", "title-bar.png",
}) | {name for name, _role, _label in _TINTED_ICONS}

_ICON_PX = 20
_ICONS_PER_ROW = 12


class ThemePreviewPanel(QWidget):
    """Right-hand live preview for the theme editor. Build once, then call
    ``refresh(working_palette)`` after every colour change / theme load."""

    rolesSelected = Signal(str, object)

    def __init__(self, parent=None):
        super().__init__(parent)
        # Manual repaint hooks, each called with the palette dict on refresh.
        self._updaters: list[Callable[[dict], None]] = []
        self._base_palette = None
        self._role_widgets: dict[QWidget, tuple[str, tuple[str, ...]]] = {}
        self._tree_roles: dict[int, tuple[str, tuple[str, ...]]] = {}
        # (item, column, bg_key, fg_key) - tree cells painted via brushes,
        # mirroring how the real modlist delegate colours its rows.
        self._tree_cells: list[tuple[QTreeWidgetItem, int, str | None, str | None]] = []

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        caption = QLabel(self.tr(
            "Preview - changes are also applied temporarily across the open "
            "app. Click any item to reveal the settings that colour it."))
        caption.setWordWrap(True)
        caption.setContentsMargins(12, 8, 12, 8)
        outer.addWidget(caption)

        self._inspector = QLabel()
        self._inspector.setObjectName("PreviewRoleInspector")
        self._inspector.setWordWrap(True)
        self._inspector.setContentsMargins(12, 7, 12, 7)
        self._inspector.hide()
        outer.addWidget(self._inspector)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        outer.addWidget(scroll, 1)

        self._content = QWidget()
        self._content.setObjectName("ThemePreviewContent")
        self._content.setAttribute(Qt.WA_StyledBackground, True)
        v = QVBoxLayout(self._content)
        v.setContentsMargins(12, 10, 12, 20)
        v.setSpacing(12)

        v.addWidget(self._build_header_section())
        v.addWidget(self._build_modlist_section())
        v.addWidget(self._build_plugins_section())
        v.addWidget(self._build_buttons_section())
        v.addWidget(self._build_inputs_section())
        v.addWidget(self._build_card_section())
        v.addWidget(self._build_status_section())
        v.addWidget(self._build_text_section())
        v.addWidget(self._build_icons_section())
        v.addStretch(1)

        scroll.setWidget(self._content)

    # ---- public -------------------------------------------------------------
    def refresh(self, pal: dict, *, restyle: bool = True) -> None:
        """Re-render the preview from *pal*. Standard widgets pick the new
        colours up from the regenerated QSS/QPalette; manually painted samples
        are repainted by the registered updaters.

        The theme editor passes ``restyle=False`` immediately before its
        app-wide live apply. This seeds the subtree's explicit palette, then the
        runtime rewrites/repolishes its tagged rules once; setting the same
        large local QSS again here would trigger a duplicate layout pass. A new
        preview has no stylesheet and is always initialised in full regardless.
        """
        p = dict(pal)   # snapshot - the editor mutates its working dict in place
        qpalette = build_qpalette(p)
        if getattr(self, "_base_palette", None) != qpalette:
            self._base_palette = qpalette
            self._content.setPalette(qpalette)
        if restyle or not self._content.styleSheet():
            self._content.setStyleSheet(build_qss(p) + self._extra_qss(p))
        for fn in self._updaters:
            fn(p)
        self._inspector.setStyleSheet(
            f"background:{_c(p, 'BG_HEADER')}; color:{_c(p, 'TEXT_MAIN')};"
            f"border-top:1px solid {_c(p, 'BORDER')};"
            f"border-bottom:1px solid {_c(p, 'BORDER')};")

    # ---- role inspector ----------------------------------------------------
    def _role_description(self, roles: tuple[str, ...]) -> str:
        return " · ".join(
            f"{self.tr(teg.role_group(key))} › "
            f"{self.tr(teg.role_label(key))} ({key})"
            for key in roles)

    def _map_roles(self, widget: QWidget, label: str,
                   roles: tuple[str, ...] | list[str]) -> None:
        mapped = tuple(dict.fromkeys(role for role in roles if role))
        if not mapped:
            return
        display = self.tr(label)
        self._role_widgets[widget] = (display, mapped)
        widget.installEventFilter(self)
        widget.setCursor(Qt.PointingHandCursor)
        widget.setToolTip(self.tr("Click to reveal theme settings:\n{0}").format(
            self._role_description(mapped)))

    def _select_roles(self, label: str, roles: tuple[str, ...]) -> None:
        self._inspector.setText(
            self.tr("{0} uses: {1}").format(
                label, self._role_description(roles)))
        self._inspector.show()
        self.rolesSelected.emit(label, roles)

    def eventFilter(self, watched, event):
        mapped = self._role_widgets.get(watched)
        if mapped and event.type() == QEvent.MouseButtonRelease:
            self._select_roles(*mapped)
        return super().eventFilter(watched, event)

    # ---- section scaffolding ------------------------------------------------
    def _extra_qss(self, p: dict) -> str:
        """Preview-only chrome build_qss doesn't cover (it has no QGroupBox /
        section styling - the app never uses one)."""
        c = lambda k: _c(p, k)
        return f"""
        #ThemePreviewContent {{ background: {c('BG_DEEP')}; }}
        #PreviewSection {{
            background: {c('BG_PANEL')};
            border: 1px solid {c('BORDER')};
            border-radius: 8px;
        }}
        #PreviewSectionTitle {{ color: {c('TEXT_MAIN')}; font-weight: 600; }}
        #PreviewCard {{
            background: {c('BG_CARD')};
            border: 1px solid {c('BORDER')};
            border-radius: 8px;
        }}
        #PreviewCardTitle {{ color: {c('TEXT_MAIN')}; font-weight: 600; }}
        """

    def _section(self, title: str) -> tuple[QFrame, QVBoxLayout]:
        frame = QFrame()
        frame.setObjectName("PreviewSection")
        lay = QVBoxLayout(frame)
        lay.setContentsMargins(10, 8, 10, 10)
        lay.setSpacing(8)
        t = QLabel(title)
        t.setObjectName("PreviewSectionTitle")
        self._map_roles(
            t, self.tr("{0} section").format(title),
            ("BG_PANEL", "BORDER", "TEXT_MAIN"))
        lay.addWidget(t)
        return frame, lay

    def _register(self, fn: Callable[[dict], None]) -> None:
        self._updaters.append(fn)

    def _inline_label(self, text: str, style: Callable[[dict], str],
                      height: int | None = None, *,
                      roles: tuple[str, ...] = (),
                      role_label: str | None = None) -> QLabel:
        """Label restyled from the palette on every refresh (mirrors the app's
        inline-styled rows: framework banner, plugin-cycle status, pills)."""
        lbl = QLabel(text)
        if height:
            lbl.setFixedHeight(height)
        self._register(lambda p, w=lbl: w.setStyleSheet(style(p)))
        if roles:
            self._map_roles(lbl, role_label or text, roles)
        return lbl

    # ---- sections -----------------------------------------------------------
    def _build_header_section(self) -> QWidget:
        frame, lay = self._section(self.tr("Header & tabs"))

        bar = QFrame()
        bar.setObjectName("HeaderBar")
        h = QHBoxLayout(bar)
        h.setContentsMargins(8, 6, 8, 6)
        h.setSpacing(6)
        for name, obj in ((self.tr("Profiles"), "ActionButton"),
                          (self.tr("Refresh"), "ActionButton"),
                          (self.tr("Save"), "PrimaryButton"),
                          (self.tr("▶ Play"), "PlayButton")):
            b = QPushButton(name)
            b.setObjectName(obj)
            b.setFocusPolicy(Qt.NoFocus)
            roles = (("BG_ROW", "TEXT_MAIN", "BORDER", "BG_ROW_HOVER")
                     if obj == "ActionButton" else
                     ("ACCENT", "ACCENT_HOV")
                     if obj == "PrimaryButton" else
                     ("BTN_SUCCESS", "BTN_SUCCESS_HOV"))
            self._map_roles(b, name, roles)
            h.addWidget(b)
        h.addStretch(1)
        self._map_roles(bar, self.tr("Header background"),
                        ("BG_HEADER", "BORDER"))
        lay.addWidget(bar)

        tabs = QTabBar()
        tabs.setDrawBase(False)
        tabs.setExpanding(False)
        tabs.setFocusPolicy(Qt.NoFocus)
        for name in (self.tr("Mods"), self.tr("Plugins"), self.tr("Data")):
            tabs.addTab(name)
        self._map_roles(
            tabs, self.tr("Tabs"),
            ("BG_HEADER", "BG_PANEL", "TEXT_DIM", "TEXT_MAIN",
             "BG_ROW_HOVER", "ACCENT"))
        lay.addWidget(tabs)
        return frame

    def _build_modlist_section(self) -> QWidget:
        frame, lay = self._section(self.tr("Mod list"))

        tree = QTreeWidget()
        tree.setColumnCount(2)
        tree.setHeaderLabels([self.tr("Mod name"), self.tr("Notes")])
        tree.setRootIsDecorated(False)
        tree.setAlternatingRowColors(True)
        tree.setFocusPolicy(Qt.NoFocus)
        tree.setEditTriggers(QTreeWidget.NoEditTriggers)
        tree.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        tree.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        no_wheel(tree)

        def add(name: str, note: str = "",
                bg: str | None = None, fg: str | None = None,
                cell_bg: str | None = None, cell_fg: str | None = None,
                roles: tuple[str, ...] = ()):
            it = QTreeWidgetItem([name, note])
            tree.addTopLevelItem(it)
            if bg or fg:
                for col in (0, 1):
                    self._tree_cells.append((it, col, bg, fg))
            if cell_bg or cell_fg:
                self._tree_cells.append((it, 1, cell_bg, cell_fg))
            mapped = tuple(dict.fromkeys(
                roles or tuple(k for k in (bg, fg, cell_bg, cell_fg) if k)
                or ("BG_ROW", "TEXT_MAIN")))
            self._tree_roles[id(it)] = (name, mapped)
            return it

        # Mirrors the bands/tints the modlist delegate paints via brushes.
        add(self.tr("Overwrite"), "", "OVERWRITE_SEP_BG", "OVERWRITE_SEP_FG")
        add(self.tr("Root Folder"), "", "ROOT_SEP_BG", "ROOT_SEP_FG")
        add(self.tr("- Gameplay -"), "", "BG_SEP", "TEXT_SEP")
        add(self.tr("Unofficial Patch"))
        sel = add(self.tr("Selected mod"), roles=("BG_SELECT", "TEXT_ON_ACCENT"))
        add(self.tr("Wins over selection"), self.tr("conflict"),
            "CONFLICT_HL_LOSE")
        add(self.tr("Loses to selection"), self.tr("conflict"),
            "CONFLICT_HL_WIN")
        add(self.tr("Plugin's mod"), self.tr("anchor"), "CONFLICT_HL_ANCHOR")
        add(self.tr("Required by selection"), self.tr("requirement"),
            "REQ_HL_REQUIRES")
        add(self.tr("Requires selection"), self.tr("requirement"),
            "REQ_HL_REQUIRED_BY")
        tree.itemClicked.connect(
            lambda item, _column: self._select_roles(
                *self._tree_roles[id(item)]))
        tree.setCurrentItem(sel)
        tree.header().setStretchLastSection(True)
        self._map_roles(
            tree.header(), self.tr("List header"),
            ("BG_HEADER", "TEXT_MAIN", "BORDER"))
        tree.setColumnWidth(0, 220)
        rows = tree.topLevelItemCount()
        tree.setFixedHeight(tree.header().sizeHint().height()
                            + rows * tree.sizeHintForRow(0) + 4)
        self._register(self._paint_tree_cells)
        lay.addWidget(tree)
        return frame

    def _paint_tree_cells(self, p: dict) -> None:
        for it, col, bg, fg in self._tree_cells:
            if bg:
                it.setBackground(col, qc(p, bg))
                if not fg:
                    # tinted band with no dedicated text key → keep it readable
                    it.setForeground(col, qc_contrast(p, bg))
            if fg:
                it.setForeground(col, qc(p, fg))

    def _build_plugins_section(self) -> QWidget:
        frame, lay = self._section(self.tr("Plugins & files"))
        row_style = lambda bg, fg: (lambda p: (
            f"background:{_c(p, bg)}; color:{_c(p, fg)};"
            f" padding-left:10px; border-radius:3px;"))

        # Framework banner rows (framework_banner.py styles these inline).
        for bg, fg, text in (
                ("FRAMEWORK_INSTALLED_BG", "FRAMEWORK_INSTALLED_FG",
                 self.tr("✔  SKSE Installed")),
                ("FRAMEWORK_STAGED_BG", "FRAMEWORK_STAGED_FG",
                 self.tr("●  SKSE present in modlist but not deployed")),
                ("FRAMEWORK_DISABLED_BG", "FRAMEWORK_DISABLED_FG",
                 self.tr("●  SKSE present in modlist but not enabled")),
                ("FRAMEWORK_MISSING_BG", "FRAMEWORK_MISSING_FG",
                 self.tr("✘  SKSE Not Present"))):
            lay.addWidget(self._inline_label(
                text, row_style(bg, fg), 22, roles=(bg, fg)))

        # Plugin-cycle status rows + rule keywords (plugin_cycle_view.py).
        for bg, fg, text in (
                ("PLUGIN_CYCLE_ERR_BG", "PLUGIN_CYCLE_ERR_FG",
                 self.tr("Cycle detected among pinned plugins")),
                ("PLUGIN_CYCLE_OK_BG", "PLUGIN_CYCLE_OK_FG",
                 self.tr("Cycle resolved")),
                ("PLUGIN_CYCLE_WARN_BG", "PLUGIN_CYCLE_WARN_FG",
                 self.tr("Flipping this rule resolves the cycle"))):
            lbl = self._inline_label(
                text, row_style(bg, fg), 22, roles=(bg, fg))
            lay.addWidget(lbl)

        words = QHBoxLayout()
        words.setSpacing(14)
        for key, text in (("PLUGIN_CYCLE_ANCHOR", self.tr("load before")),
                          ("PLUGIN_CYCLE_LINK", self.tr("load after")),
                          ("FILE_WIN", self.tr("winning file")),
                          ("FILE_LOSE", self.tr("overridden file")),
                          ("FILE_DIM", self.tr("inactive file")),
                          ("FILE_ANCHOR", self.tr("anchor file"))):
            words.addWidget(self._inline_label(
                text, lambda p, k=key: f"color:{_c(p, k)};",
                roles=(key,)))
        words.addStretch(1)
        lay.addLayout(words)

        drag = self._inline_label(
            self.tr("Drag selection outline"),
            lambda p: (f"border:2px solid {_c(p, 'HIGHLIGHT_DRAG')};"
                       f" border-radius:4px; padding:3px 8px;"),
            roles=("HIGHLIGHT_DRAG",))
        drag.setAlignment(Qt.AlignCenter)
        lay.addWidget(drag)
        return frame

    def _build_buttons_section(self) -> QWidget:
        frame, lay = self._section(self.tr("Buttons"))
        hint = QLabel(self.tr("Hover a button to preview its hover colour."))
        self._register(lambda p, w=hint: w.setStyleSheet(
            f"color:{_c(p, 'TEXT_DIM')}; font-size:11px;"))
        lay.addWidget(hint)

        grid = QGridLayout()
        grid.setSpacing(6)
        for i, (key, hov, label) in enumerate(_BUTTON_FAMILIES):
            b = QPushButton(self.tr(label))
            b.setFocusPolicy(Qt.NoFocus)
            self._register(lambda p, w=b, k=key, h=hov: w.setStyleSheet(
                button_qss(k, hover_key=h, pal=p, padding="6px 14px")))
            self._map_roles(b, self.tr(label), (key, hov))
            grid.addWidget(b, i // 4, i % 4)
        grid.setColumnStretch(4, 1)
        lay.addLayout(grid)
        return frame

    def _build_inputs_section(self) -> QWidget:
        frame, lay = self._section(self.tr("Inputs & scrollbar"))

        row = QHBoxLayout()
        row.setSpacing(10)
        edit = QLineEdit()
        edit.setPlaceholderText(self.tr("Search…"))
        self._map_roles(
            edit, self.tr("Text input"),
            ("BG_ROW", "TEXT_MAIN", "TEXT_DIM", "BORDER"))
        row.addWidget(edit, 1)
        combo = QComboBox()
        no_wheel(combo)
        combo.addItems([self.tr("Default profile"), self.tr("Testing")])
        self._map_roles(
            combo, self.tr("Dropdown"),
            ("BG_ROW", "TEXT_MAIN", "BORDER", "DROPDOWN_ARROW"))
        row.addWidget(combo, 1)
        lay.addLayout(row)

        row2 = QHBoxLayout()
        row2.setSpacing(14)
        cb_on = QCheckBox(self.tr("Enabled"))
        cb_on.setChecked(True)
        self._map_roles(
            cb_on, self.tr("Checked checkbox"),
            ("CHECK_FILL", "BORDER_FAINT", "BG_DEEP"))
        row2.addWidget(cb_on)
        cb_off = QCheckBox(self.tr("Disabled"))
        self._map_roles(
            cb_off, self.tr("Unchecked checkbox"),
            ("BORDER_FAINT", "BG_DEEP"))
        row2.addWidget(cb_off)
        rb = QRadioButton(self.tr("Selected option"))
        rb.setChecked(True)
        self._map_roles(
            rb, self.tr("Radio button"),
            ("ACCENT", "BORDER_FAINT", "BG_DEEP"))
        row2.addWidget(rb)
        row2.addStretch(1)
        lay.addLayout(row2)

        lst = QListWidget()
        lst.setFocusPolicy(Qt.NoFocus)
        lst.setAlternatingRowColors(True)
        lst.addItems([self.tr("List row {0}").format(i) for i in range(1, 13)])
        lst.setFixedHeight(110)
        list_roles = ("BG_LIST", "BG_ROW_ALT", "BG_SELECT", "TEXT_ON_ACCENT")
        lst.itemClicked.connect(
            lambda _item: self._select_roles(self.tr("List"), list_roles))
        self._map_roles(
            lst.verticalScrollBar(), self.tr("Scrollbar"),
            ("SCROLL_TROUGH", "SCROLL_BG", "SCROLL_ACTIVE"))
        lay.addWidget(lst)
        return frame

    def _build_card_section(self) -> QWidget:
        frame, lay = self._section(self.tr("Cards, toasts & progress"))

        card = QFrame()
        card.setObjectName("PreviewCard")
        self._map_roles(card, self.tr("Card background"),
                        ("BG_CARD", "BORDER"))
        cv = QVBoxLayout(card)
        cv.setContentsMargins(10, 8, 10, 8)
        cv.setSpacing(3)
        title = QLabel(self.tr("Card title"))
        title.setObjectName("PreviewCardTitle")
        self._map_roles(title, self.tr("Card title"), ("TEXT_MAIN",))
        cv.addWidget(title)
        for key, text in (("TEXT_MAIN", self.tr("Card detail text")),
                          ("TEXT_DIM", self.tr("Card secondary text"))):
            cv.addWidget(self._inline_label(
                text, lambda p, k=key: f"color:{_c(p, k)};",
                roles=(key,)))
        lay.addWidget(card)

        toast = QFrame()
        toast.setObjectName("Toast")
        self._map_roles(toast, self.tr("Toast background"),
                        ("BG_PANEL", "BORDER"))
        th = QHBoxLayout(toast)
        th.setContentsMargins(10, 6, 10, 6)
        th.setSpacing(8)
        for state, text in (("info", self.tr("Info")),
                            ("success", self.tr("Success")),
                            ("warning", self.tr("Warning")),
                            ("error", self.tr("Error"))):
            dot = QLabel("●")
            dot.setObjectName("ToastDot")
            dot.setProperty("state", state)
            dot_roles = {
                "info": ("ACCENT",),
                "success": ("TEXT_OK_BRIGHT",),
                "warning": ("TEXT_WARN_BRIGHT",),
                "error": ("STATUS_ERR_BRIGHT",),
            }
            self._map_roles(dot, self.tr("{0} toast").format(text),
                            dot_roles[state])
            th.addWidget(dot)
            toast_label = QLabel(text)
            self._map_roles(
                toast_label, self.tr("{0} toast").format(text),
                dot_roles[state])
            th.addWidget(toast_label)
        th.addStretch(1)
        lay.addWidget(toast)

        bar = QProgressBar()
        bar.setRange(0, 100)
        bar.setValue(60)
        bar.setTextVisible(False)
        bar.setFixedHeight(10)
        self._map_roles(bar, self.tr("Progress bar"),
                        ("BG_ROW", "ACCENT"))
        lay.addWidget(bar)

        for key, text in (("BG_MOD_REQ", self.tr("Required mod")),
                          ("BG_MOD_OPT", self.tr("Optional mod"))):
            lay.addWidget(self._inline_label(
                text,
                lambda p, k=key: (
                    f"background:{_c(p, k)};"
                    f" color:{contrast_text(_c(p, k))};"
                    f" padding:3px 10px; border-radius:3px;"),
                22, roles=(key,)))
        return frame

    def _build_status_section(self) -> QWidget:
        frame, lay = self._section(self.tr("Status badges"))
        row = QHBoxLayout()
        row.setSpacing(8)
        chip = QLabel(self.tr("Deployed"))
        chip.setObjectName("StatusChip")
        self._map_roles(chip, self.tr("Deployed status"), ("ACCENT",))
        row.addWidget(chip)
        for key, text in _STATUS_PILLS:
            row.addWidget(self._inline_label(
                self.tr(text),
                lambda p, k=key: (
                    f"background:{_c(p, k)};"
                    f" color:{contrast_text(_c(p, k))};"
                    f" padding:3px 8px; border-radius:3px;"),
                roles=(key,)))
        row.addStretch(1)
        lay.addLayout(row)
        return frame

    def _build_text_section(self) -> QWidget:
        frame, lay = self._section(self.tr("Text & tones"))
        grid = QGridLayout()
        grid.setHorizontalSpacing(18)
        grid.setVerticalSpacing(4)
        for i, (key, text) in enumerate(_TEXT_SAMPLES):
            deco = "text-decoration:underline;" if key == "LINK_BLUE" else ""
            grid.addWidget(self._inline_label(
                self.tr(text),
                lambda p, k=key, d=deco: f"color:{_c(p, k)}; {d}",
                roles=(key,)),
                i // 2, i % 2)
        lay.addLayout(grid)

        tones = QHBoxLayout()
        tones.setSpacing(6)
        for key in _TONES:
            chipf = QFrame()
            chipf.setFixedSize(22, 22)
            self._register(lambda p, w=chipf, k=key: w.setStyleSheet(
                f"background:{_c(p, k)}; border-radius:4px;"))
            self._map_roles(chipf, teg.role_label(key), (key,))
            tones.addWidget(chipf)
        tones.addStretch(1)
        lay.addLayout(tones)
        return frame

    def _build_icons_section(self) -> QWidget:
        """The app's icons - tinted ones re-rendered live from the palette."""
        frame, lay = self._section(self.tr("Icons"))

        themed = QLabel(self.tr("Themed - follow the palette"))
        self._register(lambda p, w=themed: w.setStyleSheet(
            f"color:{_c(p, 'TEXT_DIM')}; font-size:11px;"))
        lay.addWidget(themed)
        lay.addLayout(self._icon_grid(_TINTED_ICONS))

        fixed_names = sorted(
            (p.name for p in _ICONS_DIR.glob("*.png")
             if p.name not in _UNTINTED_EXCLUDE),
            key=str.lower)
        if fixed_names:
            fixed = QLabel(self.tr("Fixed artwork"))
            self._register(lambda p, w=fixed: w.setStyleSheet(
                f"color:{_c(p, 'TEXT_DIM')}; font-size:11px;"))
            lay.addWidget(fixed)
            lay.addLayout(self._icon_grid(
                [(name, None, name) for name in fixed_names]))
        return frame

    def _icon_grid(self, entries) -> QGridLayout:
        """Lay (filename, role or None, label) tiles out as a wrapping strip."""
        grid = QGridLayout()
        grid.setHorizontalSpacing(6)
        grid.setVerticalSpacing(6)
        for i, (name, role, label) in enumerate(entries):
            tile = QLabel()
            tile.setFixedSize(_ICON_PX + 10, _ICON_PX + 10)
            tile.setAlignment(Qt.AlignCenter)
            if role:
                self._register(
                    lambda p, w=tile, n=name, k=role: w.setPixmap(
                        _icon(n, _ICON_PX, color=_c(p, k)).pixmap(
                            _ICON_PX, _ICON_PX)))
                self._map_roles(
                    tile, self.tr("{0} ({1})").format(self.tr(label), name),
                    (role,))
            else:
                tile.setPixmap(_icon(name, _ICON_PX).pixmap(
                    _ICON_PX, _ICON_PX))
                tile.setToolTip(name)
            grid.addWidget(tile, i // _ICONS_PER_ROW, i % _ICONS_PER_ROW)
        grid.setColumnStretch(_ICONS_PER_ROW, 1)
        return grid
