"""Define-Custom-Game view — Qt port of gui/custom_game_dialog.CustomGamePanel.

Opens as a (detachable) tab. Lets the user define a brand-new game handler from
a JSON definition (name, exe, deploy type, mod sub-folder, Steam/Nexus IDs,
banner image, advanced mod-handling options, custom routing rules, framework
detection). On save the definition is written via the toolkit-neutral backend
``Games/Custom/custom_game.py`` and the caller chains to the Configure-Game tab
so the user can set the install path/prefix.

Supports both create and edit: pass an ``existing`` definition dict to
prepopulate + show a Delete button.
"""

from __future__ import annotations

import io
import threading

from PySide6.QtCore import Qt, Signal, QObject, QT_TRANSLATE_NOOP
from PySide6.QtGui import QFont

from gui_qt.wheel_guard import no_wheel
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QLabel, QLineEdit,
    QPushButton, QScrollArea, QFrame, QRadioButton, QCheckBox, QButtonGroup,
    QComboBox, QPlainTextEdit, QGroupBox,
)

from gui_qt.theme_qt import active_palette, _c
from gui_qt.help_marker import tip_text, make_help_marker, help_mark_qss
from gui_qt.collapsible_section import CollapsibleSection
from gui_qt.icons import icon, icon_rotated
from gui_qt.safe_emit import safe_emit
from Games.Custom.custom_game import (
    _make_game_id,
    decode_custom_game_definition,
    delete_custom_game_definition,
    encode_custom_game_definition,
    load_builtin_game_templates,
    load_custom_game_definitions,
    make_custom_game,
    save_custom_game_definition,
)
from Utils.config_paths import get_custom_game_images_dir


# Runtime passthrough used in the local _adv_fields list. The strings are
# canonical English, translated at DISPLAY time via self.tr(...). _T is a no-op
# at runtime; the literals are registered for lupdate in _TR_MARKERS below
# (lupdate can't extract through _T, so that explicit list is the source of
# truth for which strings land in the CustomGameView context).
def _T(s: str) -> str:
    return s


# Deploy-type radios: (label, value, description) — mirrors the Tk dialog.
_DEPLOY_OPTIONS = [
    ("Standard", "standard",
     "Mods install into a single sub-folder (e.g. Data/, BepInEx/plugins/). "
     "Same as Bethesda games and BepInEx."),
    ("Root", "root",
     "Mods deploy directly to the game's root folder. "
     "Same as The Witcher 3 and Cyberpunk 2077."),
    ("UE5", "ue5",
     "Unreal Engine 5 — pak files → Content/Paks/~mods/, UE4SS/lua → "
     "Binaries/Win64/, DLLs → Binaries/Win64/. Same routing as Hogwarts "
     "Legacy / Oblivion Remastered."),
]

# Display-label ↔ stored-value mapping for the filemap_casing dropdown. Labels
# translated at display time; the combo carries `value` as item-data so the
# round-trip never depends on the (translated) visible text.
_FILEMAP_CASING_OPTIONS = [
    ("Most uppercase",       "upper"),
    ("Most lowercase",       "lower"),
    ("Lowercase everything", "force_lower"),
    ("Uppercase everything", "force_upper"),
]

# lupdate extraction anchors for the strings in the tables above + the advanced
# fields/toggles (which live in local lists inside _build). QT_TRANSLATE_NOOP
# takes the LITERAL directly (it can't see through _T / a loop var); this list
# is what puts them in the CustomGameView context so self.tr() finds them.
_TR_MARKERS = (
    QT_TRANSLATE_NOOP("CustomGameView", "Standard"),
    QT_TRANSLATE_NOOP("CustomGameView", "Mods install into a single sub-folder (e.g. Data/, BepInEx/plugins/). "
       "Same as Bethesda games and BepInEx."),
    QT_TRANSLATE_NOOP("CustomGameView", "Root"),
    QT_TRANSLATE_NOOP("CustomGameView", "Mods deploy directly to the game's root folder. "
       "Same as The Witcher 3 and Cyberpunk 2077."),
    QT_TRANSLATE_NOOP("CustomGameView", "UE5"),
    QT_TRANSLATE_NOOP("CustomGameView", "Unreal Engine 5 — pak files → Content/Paks/~mods/, UE4SS/lua → "
       "Binaries/Win64/, DLLs → Binaries/Win64/. Same routing as Hogwarts "
       "Legacy / Oblivion Remastered."),
    QT_TRANSLATE_NOOP("CustomGameView", "Most uppercase"), QT_TRANSLATE_NOOP("CustomGameView", "Most lowercase"),
    QT_TRANSLATE_NOOP("CustomGameView", "Lowercase everything"), QT_TRANSLATE_NOOP("CustomGameView", "Uppercase everything"),
    QT_TRANSLATE_NOOP("CustomGameView", "Strip Prefixes"),
    QT_TRANSLATE_NOOP("CustomGameView", "Comma-separated top-level folder names to strip from mod files "
       "during filemap building (case-insensitive). e.g. Data, data"),
    QT_TRANSLATE_NOOP("CustomGameView", "Prepend Prefix"),
    QT_TRANSLATE_NOOP("CustomGameView", "Path segment prepended to every installed file. "
       "e.g. 'mods' so files land at mods/<ModName>/…"),
    QT_TRANSLATE_NOOP("CustomGameView", "Required Top-Level Folders"),
    QT_TRANSLATE_NOOP("CustomGameView", "Comma-separated folder names a mod must contain at its root. "
       "If none match, the user is prompted to set a data directory."),
    QT_TRANSLATE_NOOP("CustomGameView", "Required File Types"),
    QT_TRANSLATE_NOOP("CustomGameView", "Comma-separated file extensions a mod must contain at its root. "
       "e.g. .esp, .esm — works standalone or as a fallback after "
       "Required Top-Level Folders."),
    QT_TRANSLATE_NOOP("CustomGameView", "Strip Prefixes (post-install)"),
    QT_TRANSLATE_NOOP("CustomGameView", "Like Strip Prefixes but applied after Required Top-Level Folders "
       "validation. e.g. reframework"),
    QT_TRANSLATE_NOOP("CustomGameView", "Conflict Ignore Filenames"),
    QT_TRANSLATE_NOOP("CustomGameView", "Comma-separated filenames excluded from conflict detection. "
       "Supports glob patterns: *.<ext> matches any file with that "
       "extension, <name>.* matches that name with any extension. "
       "e.g. modinfo.ini, manifest.json, *.txt, LICENCE.*"),
    QT_TRANSLATE_NOOP("CustomGameView", "Auto Strip Until Required"),
    QT_TRANSLATE_NOOP("CustomGameView", "When enabled and Required Top-Level Folders is set, strip "
       "leading path segments automatically instead of prompting the user."),
    QT_TRANSLATE_NOOP("CustomGameView", "Install As-Is If No Match"),
    QT_TRANSLATE_NOOP("CustomGameView", "When enabled, if both Required Top-Level Folders and "
       "Required File Types checks fail, the mod is installed "
       "as-is without showing the prefix dialog."),
    QT_TRANSLATE_NOOP("CustomGameView", "Restore Before Deploy"),
    QT_TRANSLATE_NOOP("CustomGameView", "When enabled (default), the manager runs Restore before every "
       "Deploy to clean the game state first. Disable only if the game's "
       "deploy cycle handles its own cleanup internally."),
    QT_TRANSLATE_NOOP("CustomGameView", "Normalize Folder Case"),
    QT_TRANSLATE_NOOP("CustomGameView", "When enabled (default), folder names that differ only in case "
       "across mods are unified to a single casing. Disable for "
       "Linux-native games where folder casing is significant."),
)


# --- dialog ↔ JSON value helpers (ported from custom_game_dialog.py) -------

def _set_to_str(value) -> str:
    if isinstance(value, (list, set)):
        return ", ".join(str(v) for v in value)
    return str(value) if value else ""


def _dll_to_str(value) -> str:
    if isinstance(value, dict):
        return "\n".join(f"{k}={v}" for k, v in value.items())
    return str(value) if value else ""


def _str_to_list(text: str) -> list[str]:
    return [s.strip() for s in text.split(",") if s.strip()]


def _parse_dll_text(text: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if "=" in line:
            k, _, v = line.partition("=")
            if k.strip():
                result[k.strip()] = v.strip()
    return result


class _ImageSignals(QObject):
    status = Signal(str, str)   # (text, tone_key)


class CustomGameView(QWidget):
    """*on_done(saved_defn: dict | None, deleted: bool)* is called after
    Save/Delete/Cancel so the window can refresh the game list and close the
    tab. ``saved_defn`` is None on cancel/delete."""

    _TYPE_COMBO_W = 110   # fixed width of the routing match-type combo

    def __init__(self, on_done, existing: dict | None = None, parent=None):
        super().__init__(parent)
        self._on_done = on_done or (lambda saved, deleted: None)
        self._existing = existing
        self._p = active_palette()

        # Dynamic-table row state.
        self._routing_rows: list[dict] = []
        self._whitelist_rows: list[dict] = []
        self._framework_rows: list[dict] = []

        self._img_sig = _ImageSignals()
        self._img_sig.status.connect(self._set_image_status)

        self._build()
        self._prepopulate()
        self._update_data_path_visibility()

    # ---- styling helpers --------------------------------------------------
    def _c(self, k):
        return _c(self._p, k)

    def _qss(self) -> str:
        c = self._c
        return f"""
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
        #CollapsibleSection {{
            border: 1px solid {c('BORDER')};
            border-radius: 6px;
            background: {c('BG_PANEL')};
        }}
        QToolButton#SectionToggle {{
            background: transparent; border: none;
            font-weight: bold; color: {c('TEXT_MAIN')};
            padding: 2px 0;
        }}
        QToolButton#SectionToggle:hover,
        QToolButton#SectionToggle:pressed,
        QToolButton#SectionToggle:checked {{
            background: transparent; color: {c('TEXT_MAIN')};
        }}
        {help_mark_qss(self._p)}
        """

    def _mono_edit(self, placeholder: str = "") -> QLineEdit:
        e = QLineEdit()
        e.setObjectName("PathEdit")
        f = QFont("monospace"); f.setStyleHint(QFont.Monospace); e.setFont(f)
        if placeholder:
            e.setPlaceholderText(placeholder)
        return e

    # ---- section + row builders (settings_view look) ----------------------
    def _section(self, title: str) -> QGridLayout:
        """Add a QGroupBox to the body and return its 2-col grid."""
        box = QGroupBox(title.replace("&", "&&"))  # bare & = mnemonic
        grid = self._make_grid(box)
        grid.setContentsMargins(8, 6, 8, 6)
        self._body_v.addWidget(box)
        return grid

    def _collapsible(self, title: str, tip: str = "") -> CollapsibleSection:
        sec = CollapsibleSection(title, tip)
        self._body_v.addWidget(sec)
        return sec

    def _make_grid(self, parent_widget) -> QGridLayout:
        """The shared 2-col grid recipe (label col | stretching field col)."""
        grid = QGridLayout(parent_widget)
        grid.setContentsMargins(0, 2, 0, 2)
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(8)
        grid.setColumnStretch(0, 0)
        grid.setColumnStretch(1, 1)
        grid.setProperty("_row", 0)
        return grid

    def _next_row(self, grid: QGridLayout) -> int:
        r = int(grid.property("_row") or 0)
        grid.setProperty("_row", r + 1)
        return r

    def _field_row(self, grid: QGridLayout, label: str, field, hint: str = "",
                   stretch: bool = True, align_top: bool = False):
        """label | field row; `hint` becomes a tooltip + "?" marker.

        Returns (label, marker) so dynamic rows (Mod Sub-folder) can retitle
        and re-tip themselves. `stretch=False` keeps the field at its natural
        width (combos/buttons)."""
        row = self._next_row(grid)
        lbl = QLabel(label)
        if align_top:
            grid.addWidget(lbl, row, 0, Qt.AlignTop)
        else:
            grid.addWidget(lbl, row, 0)
        if isinstance(field, QLineEdit):
            field.setMinimumWidth(180)
        wrap = QHBoxLayout()
        wrap.setContentsMargins(0, 0, 0, 0)
        wrap.setSpacing(6)
        wrap.addWidget(field, 1 if stretch else 0)
        mark = None
        if hint:
            tip = tip_text(hint)
            lbl.setToolTip(tip)
            field.setToolTip(tip)
            mark = make_help_marker(hint)
            wrap.addWidget(mark)
        if not stretch:
            wrap.addStretch(1)
        holder = QWidget(); holder.setLayout(wrap)
        grid.addWidget(holder, row, 1)
        return lbl, mark

    def _check_row(self, grid: QGridLayout, cb: QCheckBox, hint: str = ""):
        """A checkbox spanning both columns, with tooltip + "?" marker."""
        row = self._next_row(grid)
        if hint:
            cb.setToolTip(tip_text(hint))
            wrap = QHBoxLayout()
            wrap.setContentsMargins(0, 0, 0, 0)
            wrap.setSpacing(6)
            wrap.addWidget(cb)
            wrap.addWidget(make_help_marker(hint))
            wrap.addStretch(1)
            holder = QWidget(); holder.setLayout(wrap)
            grid.addWidget(holder, row, 0, 1, 2)
        else:
            grid.addWidget(cb, row, 0, 1, 2)

    # ---- build ------------------------------------------------------------
    def _build(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # Title bar.
        header = QWidget(); header.setObjectName("HeaderBar")
        hb = QHBoxLayout(header); hb.setContentsMargins(12, 8, 12, 8)
        title = QLabel(self.tr("Edit Custom Game") if self._existing else self.tr("Define Custom Game"))
        title.setStyleSheet("font-size:15px; font-weight:600;")
        hb.addWidget(title); hb.addStretch(1)
        if self._existing:
            export_btn = QPushButton(self.tr("Export code…"))
            export_btn.setObjectName("FormButton")
            export_btn.setCursor(Qt.PointingHandCursor)
            export_btn.clicked.connect(self._on_export_code)
            hb.addWidget(export_btn)
        outer.addWidget(header)

        # Scrollable body.
        scroll = QScrollArea(); scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        body = QWidget(); body.setObjectName("FormBody")
        v = QVBoxLayout(body); v.setContentsMargins(16, 14, 16, 14); v.setSpacing(14)
        scroll.setWidget(body)
        outer.addWidget(scroll, 1)
        self._body_v = v
        self.setStyleSheet(self._qss())

        # --- Load preset (create mode only) ---
        if not self._existing:
            g = self._section(self.tr("Load Preset"))
            self._preset_combo = QComboBox()
            self._preset_combo.setMaxVisibleItems(15)
            self._preset_combo.setStyleSheet("QComboBox { combobox-popup: 0; }")
            self._preset_combo.addItem(self.tr("— Select a game to copy from —"), userData=None)
            for defn in load_builtin_game_templates():
                nm = defn.get("name", "")
                if nm:
                    self._preset_combo.addItem(
                        self.tr("{0}  (built-in template)").format(nm), userData=defn)
            for defn in load_custom_game_definitions():
                nm = defn.get("name", "")
                if nm:
                    self._preset_combo.addItem(nm, userData=defn)
            no_wheel(self._preset_combo)
            self._preset_combo.currentIndexChanged.connect(self._on_preset_selected)
            self._field_row(
                g, self.tr("Preset"), self._preset_combo,
                self.tr("Prepopulate the fields below from an existing custom game "
                "as a starting template. You still need to give the new game its "
                "own unique name."))

            import_btn = QPushButton(self.tr("Import code…"))
            import_btn.setObjectName("FormButton")
            import_btn.setCursor(Qt.PointingHandCursor)
            import_btn.clicked.connect(self._on_import_code)
            self._field_row(
                g, self.tr("Share code"), import_btn,
                self.tr("Or paste a share code exported from another custom game "
                "to prefill every field below."), stretch=False)

        # --- Basics ---
        g = self._section(self.tr("Basics"))
        self._name_edit = QLineEdit()
        self._name_edit.setPlaceholderText(self.tr("e.g. My Favourite Game"))
        self._field_row(
            g, self.tr("Game Name"), self._name_edit,
            self.tr("The display name shown in the game selector "
                    "(must be unique)."))
        self._exe_edit = self._mono_edit(self.tr("e.g. MyGame.exe"))
        self._field_row(
            g, self.tr("Executable Filename"), self._exe_edit,
            self.tr("The .exe location from the game's root folder. e.g. bin/bg3.exe "
            "for BG3 or SkyrimSELauncher.exe for Skyrim SE"))

        # --- Deployment ---
        g = self._section(self.tr("Deployment"))
        self._deploy_group = QButtonGroup(self)
        self._deploy_buttons: dict[str, QRadioButton] = {}
        # Radios one per row in the field column, "Deploy Method" labelling the
        # first row; each radio carries its own description marker.
        label_row = self._next_row(g)
        g.addWidget(QLabel(self.tr("Deploy Method")), label_row, 0)
        first = True
        for label, value, desc in _DEPLOY_OPTIONS:
            rb = QRadioButton(self.tr(label))
            self._deploy_group.addButton(rb)
            self._deploy_buttons[value] = rb
            rb.toggled.connect(self._update_data_path_visibility)
            d = self.tr(desc)
            rb.setToolTip(tip_text(d))
            wrap = QHBoxLayout()
            wrap.setContentsMargins(0, 0, 0, 0)
            wrap.setSpacing(6)
            wrap.addWidget(rb)
            wrap.addWidget(make_help_marker(d))
            wrap.addStretch(1)
            holder = QWidget(); holder.setLayout(wrap)
            g.addWidget(holder, label_row if first else self._next_row(g), 1)
            first = False
        self._deploy_buttons["standard"].setChecked(True)

        # Mod Sub-folder (standard / ue5; disabled for root). Title, tooltip
        # and placeholder are set by _update_data_path_visibility.
        self._data_path_edit = self._mono_edit()
        self._dp_label, self._dp_mark = self._field_row(
            g, self.tr("Mod Sub-folder"), self._data_path_edit,
            self.tr("Path relative to the game root where mod files are installed. "
            "e.g. 'Data' for Bethesda games, 'BepInEx/plugins' for BepInEx. "
            "Leave empty to target the game root directly."))

        # --- Store & Artwork ---
        g = self._section(self.tr("Store & Artwork"))
        self._steam_edit = self._mono_edit(self.tr("e.g. 377160"))
        self._field_row(
            g, self.tr("Steam App ID"), self._steam_edit,
            self.tr("Used to auto-detect the Proton prefix. Leave "
                    "empty if not on Steam."))
        self._nexus_edit = self._mono_edit(self.tr("e.g. myfavouritegame"))
        self._field_row(
            g, self.tr("Nexus Mods Domain"), self._nexus_edit,
            self.tr("The game's slug on nexusmods.com. "
                    "e.g. 'skyrimspecialedition'."))
        self._image_edit = self._mono_edit(self.tr("https://example.com/banner.jpg"))
        self._field_row(
            g, self.tr("Banner Image URL"), self._image_edit,
            self.tr("A direct URL to a PNG/JPG image shown in the game picker card. "
            "The image is downloaded once and cached locally."))
        self._image_status = QLabel("")
        self._image_status.setStyleSheet(f"color:{self._c('TEXT_DIM')};")
        g.addWidget(self._image_status, self._next_row(g), 0, 1, 2)

        # --- Mod Install Tuning (collapsed) ---
        self._adv_edits: dict[str, QLineEdit] = {}
        self._adv_toggles: dict[str, QCheckBox] = {}

        def _render_entry(grid, key, label, hint):
            e = self._mono_edit()
            self._field_row(grid, self.tr(label), e, self.tr(hint))
            self._adv_edits[key] = e

        def _render_toggle(grid, key, label, hint, default=False):
            cb = QCheckBox(self.tr(label))
            cb.setChecked(default)
            self._check_row(grid, cb, self.tr(hint))
            self._adv_toggles[key] = cb

        self._sec_tuning = self._collapsible(
            self.tr("Mod Install Tuning"),
            self.tr("Used to change the folder structure of an installed mod to match "
            "what is required by the manager."))
        g = self._make_grid(self._sec_tuning.body)
        # Rows follow the install-pipeline ordering: the two Required-Top-Level
        # decision toggles sit right after Required File Types.
        _render_entry(
            g, "mod_folder_strip_prefixes", _T("Strip Prefixes"),
            _T("Comma-separated top-level folder names to strip from mod files "
               "during filemap building (case-insensitive). e.g. Data, data"))
        _render_entry(
            g, "mod_install_prefix", _T("Prepend Prefix"),
            _T("Path segment prepended to every installed file. "
               "e.g. 'mods' so files land at mods/<ModName>/…"))
        _render_entry(
            g, "mod_required_top_level_folders", _T("Required Top-Level Folders"),
            _T("Comma-separated folder names a mod must contain at its root. "
               "If none match, the user is prompted to set a data directory."))
        _render_entry(
            g, "mod_required_file_types", _T("Required File Types"),
            _T("Comma-separated file extensions a mod must contain at its root. "
               "e.g. .esp, .esm — works standalone or as a fallback after "
               "Required Top-Level Folders."))
        _render_toggle(
            g, "mod_auto_strip_until_required", _T("Auto Strip Until Required"),
            _T("When enabled and Required Top-Level Folders is set, strip "
               "leading path segments automatically instead of prompting "
               "the user."))
        _render_toggle(
            g, "mod_install_as_is_if_no_match", _T("Install As-Is If No Match"),
            _T("When enabled, if both Required Top-Level Folders and "
               "Required File Types checks fail, the mod is installed "
               "as-is without showing the prefix dialog."))
        _render_entry(
            g, "mod_folder_strip_prefixes_post", _T("Strip Prefixes (post-install)"),
            _T("Like Strip Prefixes but applied after Required Top-Level Folders "
               "validation. e.g. reframework"))

        # --- Conflicts & Advanced Behaviour (collapsed) ---
        self._sec_conflicts = self._collapsible(
            self.tr("Conflicts & Advanced Behaviour"))
        g = self._make_grid(self._sec_conflicts.body)
        _render_entry(
            g, "conflict_ignore_filenames", _T("Conflict Ignore Filenames"),
            _T("Comma-separated filenames excluded from conflict detection. "
               "Supports glob patterns: *.<ext> matches any file with that "
               "extension, <name>.* matches that name with any extension. "
               "e.g. modinfo.ini, manifest.json, *.txt, LICENCE.*"))
        _render_entry(
            g, "conflict_ignore_foldernames", _T("Conflict Ignore Folder Names"),
            _T("Comma-separated folder names excluded from deployment and "
               "conflict detection. A folder whose name matches (at any "
               "depth) is skipped along with everything inside it. Supports "
               "glob patterns. e.g. docs, screenshots, *_backup"))
        _render_toggle(
            g, "restore_before_deploy", _T("Restore Before Deploy"),
            _T("When enabled (default), the manager runs Restore before every "
               "Deploy to clean the game state first. Disable only if the game's "
               "deploy cycle handles its own cleanup internally."), default=True)
        _render_toggle(
            g, "normalize_folder_case", _T("Normalize Folder Case"),
            _T("When enabled (default), folder names that differ only in case "
               "across mods are unified to a single casing. Disable for "
               "Linux-native games where folder casing is significant."),
            default=True)

        # Filemap casing strategy.
        self._casing_combo = QComboBox()
        for label, val in _FILEMAP_CASING_OPTIONS:
            # Visible text translated; the stable value rides as item-data so
            # save/load never keys off the (translatable) label.
            self._casing_combo.addItem(self.tr(label), userData=val)
        no_wheel(self._casing_combo)
        self._field_row(
            g, self.tr("Filemap Casing"), self._casing_combo,
            self.tr("How to pick canonical folder casing when mods disagree. "
            "Only used when Normalize Folder Case is enabled."), stretch=False)

        # Wine DLL overrides (multi-line).
        self._dll_edit = QPlainTextEdit()
        self._dll_edit.setFixedHeight(72)
        f = QFont("monospace"); f.setStyleHint(QFont.Monospace); self._dll_edit.setFont(f)
        self._field_row(
            g, self.tr("Wine DLL Overrides"), self._dll_edit,
            self.tr("One override per line: dll_name=load_order  "
                    "e.g. winhttp=native,builtin"), align_top=True)

        # --- Custom Routing Rules (collapsed) ---
        self._sec_routing = self._collapsible(
            self.tr("Custom Routing Rules"),
            self.tr("Route specific files to alternate destinations during deploy. "
            "Each rule maps files (by extension, folder or filename) to a "
            "game-root-relative directory. For extensions, append (.ext, .ext) "
            "to also route same-stem siblings (e.g. .asi (.ini) sends Foo.ini "
            "alongside Foo.asi). Flatten drops subfolders below the matched "
            "folder. To Prefix routes relative to the Proton/Wine prefix root "
            "instead of the game install root."))
        v = QVBoxLayout(self._sec_routing.body)
        v.setContentsMargins(0, 2, 0, 2)
        v.setSpacing(6)
        add_rule = QPushButton(self.tr("+ Add Rule"))
        add_rule.setObjectName("FormButton")
        add_rule.setCursor(Qt.PointingHandCursor)
        add_rule.clicked.connect(lambda: self._add_routing_rule())
        v.addWidget(add_rule, alignment=Qt.AlignLeft)
        self._routing_container = QWidget()
        self._routing_vbox = QVBoxLayout(self._routing_container)
        self._routing_vbox.setContentsMargins(0, 0, 0, 0)
        self._routing_vbox.setSpacing(2)
        # Column headers over the dest / match-value inputs — shown only while
        # at least one rule row exists. Stretch factors mirror the row layout.
        self._routing_header = QWidget()
        rh = QHBoxLayout(self._routing_header)
        rh.setContentsMargins(4, 0, 4, 0); rh.setSpacing(4)
        rh.addSpacing(24 + 4)               # up/down button column
        dest_lbl = QLabel(self.tr("Destination"))
        dest_lbl.setStyleSheet(f"color:{self._c('TEXT_DIM')};")
        file_lbl = QLabel(self.tr("File/folder"))
        file_lbl.setStyleSheet(f"color:{self._c('TEXT_DIM')};")
        rh.addWidget(dest_lbl, 1)
        rh.addSpacing(self._TYPE_COMBO_W)   # match-type combo column
        rh.addWidget(file_lbl, 1)
        rh.addStretch(0)
        self._routing_header.setVisible(False)
        v.addWidget(self._routing_header)
        v.addWidget(self._routing_container)

        # --- Restore Whitelist (collapsed) ---
        self._sec_whitelist = self._collapsible(
            self.tr("Restore Whitelist"),
            self.tr("Protect runtime-generated files from being moved out of the "
            "game folder on restore. Each rule anchors at a path relative to "
            "the game root (empty = the game root) and matches folder names "
            "(protecting the folder's whole contents), filenames, or "
            "extensions directly at that path. Matching is case-insensitive "
            "and anchored — the same name at any other path needs its own "
            "rule. Folder and filename values accept wildcards (e.g. "
            "ego_dlc* or *.log)."))
        v = QVBoxLayout(self._sec_whitelist.body)
        v.setContentsMargins(0, 2, 0, 2)
        v.setSpacing(6)
        add_wl = QPushButton(self.tr("+ Add Rule"))
        add_wl.setObjectName("FormButton")
        add_wl.setCursor(Qt.PointingHandCursor)
        add_wl.clicked.connect(lambda: self._add_whitelist_rule())
        v.addWidget(add_wl, alignment=Qt.AlignLeft)
        self._whitelist_container = QWidget()
        self._whitelist_vbox = QVBoxLayout(self._whitelist_container)
        self._whitelist_vbox.setContentsMargins(0, 0, 0, 0)
        self._whitelist_vbox.setSpacing(2)
        self._whitelist_header = QWidget()
        wh = QHBoxLayout(self._whitelist_header)
        wh.setContentsMargins(4, 0, 4, 0); wh.setSpacing(4)
        wh.addSpacing(24 + 4)               # up/down button column
        wl_path_lbl = QLabel(self.tr("Path in game root"))
        wl_path_lbl.setStyleSheet(f"color:{self._c('TEXT_DIM')};")
        wl_file_lbl = QLabel(self.tr("File/folder"))
        wl_file_lbl.setStyleSheet(f"color:{self._c('TEXT_DIM')};")
        wh.addWidget(wl_path_lbl, 1)
        wh.addSpacing(self._TYPE_COMBO_W)   # match-type combo column
        wh.addWidget(wl_file_lbl, 1)
        wh.addStretch(0)
        self._whitelist_header.setVisible(False)
        v.addWidget(self._whitelist_header)
        v.addWidget(self._whitelist_container)

        # --- Framework Detection (collapsed) ---
        self._sec_frameworks = self._collapsible(
            self.tr("Framework Detection"),
            self.tr("Display a status banner in the Plugins tab when a framework is "
            "installed. Enter the framework name on the left and its file path "
            "relative to the game root on the right."))
        v = QVBoxLayout(self._sec_frameworks.body)
        v.setContentsMargins(0, 2, 0, 2)
        v.setSpacing(6)
        add_fw = QPushButton(self.tr("+ Add Framework"))
        add_fw.setObjectName("FormButton")
        add_fw.setCursor(Qt.PointingHandCursor)
        add_fw.clicked.connect(lambda: self._add_framework())
        v.addWidget(add_fw, alignment=Qt.AlignLeft)
        self._framework_container = QWidget()
        self._framework_vbox = QVBoxLayout(self._framework_container)
        self._framework_vbox.setContentsMargins(0, 0, 0, 0)
        self._framework_vbox.setSpacing(2)
        v.addWidget(self._framework_container)

        # Validation label.
        self._validation = QLabel("")
        self._validation.setWordWrap(True)
        self._validation.setStyleSheet(f"color:{self._c('TEXT_ERR')};")
        self._body_v.addWidget(self._validation)

        self._body_v.addStretch(1)

        # --- Button bar ---
        bar = QWidget(); bar.setObjectName("BottomBar")
        bb = QHBoxLayout(bar); bb.setContentsMargins(12, 8, 12, 8)
        if self._existing:
            del_btn = QPushButton(self.tr("Delete"))
            del_btn.setObjectName("DangerButton")
            del_btn.setCursor(Qt.PointingHandCursor)
            del_btn.clicked.connect(self._on_delete)
            bb.addWidget(del_btn)
        bb.addStretch(1)
        self._save_btn = QPushButton(self.tr("Save Game"))
        self._save_btn.setObjectName("PrimaryButton")
        self._save_btn.setCursor(Qt.PointingHandCursor)
        self._save_btn.clicked.connect(self._on_save)
        bb.addWidget(self._save_btn)
        cancel = QPushButton(self.tr("Cancel"))
        cancel.setObjectName("FormButton")
        cancel.setCursor(Qt.PointingHandCursor)
        cancel.clicked.connect(lambda: self._on_done(None, False))
        bb.addWidget(cancel)
        outer.addWidget(bar)

    # ---- deploy-type-dependent sub-folder row -----------------------------
    def _update_data_path_visibility(self, *_):
        # The deploy radios' toggled signal can fire during _build() before the
        # sub-folder widgets exist; ignore until they're created.
        if not hasattr(self, "_dp_label"):
            return
        deploy = self._current_deploy_type()
        enabled = deploy != "root"
        self._dp_label.setEnabled(enabled)
        self._dp_mark.setEnabled(enabled)
        self._data_path_edit.setEnabled(enabled)
        if deploy == "ue5":
            self._dp_label.setText(self.tr("Game Sub-folder"))
            t = self.tr("Location of the folder from root where deployed mods are sent "
                "to. e.g. Phoenix for Hogwarts Legacy.")
            self._data_path_edit.setPlaceholderText(self.tr("e.g. OblivionRemastered"))
        else:
            self._dp_label.setText(self.tr("Mod Sub-folder"))
            t = self.tr("Path relative to the game root where mod files are installed. "
                "e.g. 'Data' for Bethesda games, 'BepInEx/plugins' for BepInEx. "
                "Leave empty to target the game root directly.")
            self._data_path_edit.setPlaceholderText(
                self.tr("e.g. Data   (leave empty for game root)"))
        tip = tip_text(t)
        self._dp_label.setToolTip(tip)
        self._dp_mark.setToolTip(tip)
        self._data_path_edit.setToolTip(tip)

    def _current_deploy_type(self) -> str:
        for value, rb in self._deploy_buttons.items():
            if rb.isChecked():
                return value
        return "standard"

    # ---- routing-rule rows ------------------------------------------------
    def _add_routing_rule(self, dest="", match_type="extensions", match_value="",
                          loose_only=False, flatten=False, include_siblings=False,
                          to_prefix=False):
        row = QFrame(); row.setObjectName("RuleRow")
        row.setFrameShape(QFrame.StyledPanel)
        hb = QHBoxLayout(row); hb.setContentsMargins(4, 4, 4, 4); hb.setSpacing(4)

        up = QPushButton(); up.setFixedWidth(24)
        up.setIcon(icon_rotated("arrow.png", 180, 12, "#ffffff"))
        up.setToolTip(self.tr("Move up"))
        down = QPushButton(); down.setFixedWidth(24)
        down.setIcon(icon_rotated("arrow.png", 0, 12, "#ffffff"))
        down.setToolTip(self.tr("Move down"))

        dest_edit = self._mono_edit(self.tr("Destination"))
        type_combo = QComboBox()
        # Visible text translated; canonical value ("extensions"/"folders"/
        # "filenames") rides as item-data so save/compare never keys off the
        # translated label.
        for _val, _lbl in (("extensions", self.tr("extensions")),
                           ("folders", self.tr("folders")),
                           ("filenames", self.tr("filenames"))):
            type_combo.addItem(_lbl, userData=_val)
        _ti = type_combo.findData(match_type)
        type_combo.setCurrentIndex(_ti if _ti >= 0 else 0)
        type_combo.setFixedWidth(self._TYPE_COMBO_W)
        no_wheel(type_combo)
        value_edit = self._mono_edit(self.tr("File/Folder"))
        dest_edit.setText(dest)
        value_edit.setText(match_value)

        cb_loose = QCheckBox(self.tr("Loose only")); cb_loose.setChecked(loose_only)
        cb_flat = QCheckBox(self.tr("Flatten")); cb_flat.setChecked(flatten)
        cb_sib = QCheckBox(self.tr("Include Siblings")); cb_sib.setChecked(include_siblings)
        cb_pfx = QCheckBox(self.tr("To Prefix")); cb_pfx.setChecked(to_prefix)

        remove = QPushButton(); remove.setObjectName("DangerButton")
        remove.setIcon(icon("close_white.png", 12))
        remove.setToolTip(self.tr("Remove rule"))
        remove.setFixedWidth(28)

        vbtns = QVBoxLayout(); vbtns.setSpacing(0); vbtns.setContentsMargins(0, 0, 0, 0)
        vbtns.addWidget(up); vbtns.addWidget(down)
        hb.addLayout(vbtns)
        hb.addWidget(dest_edit, 1)
        hb.addWidget(type_combo)
        hb.addWidget(value_edit, 1)
        hb.addWidget(cb_loose); hb.addWidget(cb_flat)
        hb.addWidget(cb_sib); hb.addWidget(cb_pfx)
        hb.addWidget(remove)

        rd = {"frame": row, "dest": dest_edit, "type": type_combo,
              "value": value_edit, "loose_only": cb_loose, "flatten": cb_flat,
              "include_siblings": cb_sib, "to_prefix": cb_pfx}
        self._routing_rows.append(rd)
        self._routing_vbox.addWidget(row)
        self._routing_header.setVisible(True)

        up.clicked.connect(lambda: self._move_routing_rule(rd, -1))
        down.clicked.connect(lambda: self._move_routing_rule(rd, 1))
        remove.clicked.connect(lambda: self._remove_routing_rule(rd))

    def _remove_routing_rule(self, rd):
        if rd in self._routing_rows:
            self._routing_rows.remove(rd)
            self._routing_vbox.removeWidget(rd["frame"])
            rd["frame"].deleteLater()
            if not self._routing_rows:
                self._routing_header.setVisible(False)

    def _move_routing_rule(self, rd, delta):
        rows = self._routing_rows
        if rd not in rows:
            return
        i = rows.index(rd)
        j = i + delta
        if j < 0 or j >= len(rows):
            return
        rows[i], rows[j] = rows[j], rows[i]
        self._routing_vbox.removeWidget(rd["frame"])
        self._routing_vbox.insertWidget(j, rd["frame"])

    def _collect_routing_rules(self) -> list[dict]:
        rules = []
        for rd in self._routing_rows:
            dest = rd["dest"].text().strip()
            match_type = rd["type"].currentData()
            raw_value = rd["value"].text().strip()

            companions: list[str] = []
            if match_type == "extensions" and "(" in raw_value and ")" in raw_value:
                before, _, rest = raw_value.partition("(")
                inside, _, after = rest.partition(")")
                companions = [v.strip() for v in inside.split(",") if v.strip()]
                raw_value = (before + " " + after).strip().rstrip(",").strip()

            values = [v.strip() for v in raw_value.split(",") if v.strip()]
            if not values and not dest:
                continue
            rule: dict = {"dest": dest}
            if match_type == "extensions":
                rule["extensions"] = values
                if companions:
                    rule["companion_extensions"] = companions
            elif match_type == "filenames":
                rule["filenames"] = values
            else:
                rule["folders"] = values
            if rd["loose_only"].isChecked():
                rule["loose_only"] = True
            if rd["flatten"].isChecked():
                rule["flatten"] = True
            if rd["include_siblings"].isChecked():
                rule["include_siblings"] = True
            if rd["to_prefix"].isChecked():
                rule["to_prefix"] = True
            rules.append(rule)
        return rules

    # ---- restore-whitelist rows --------------------------------------------
    def _add_whitelist_rule(self, path="", match_type="folders", match_value=""):
        row = QFrame(); row.setObjectName("RuleRow")
        row.setFrameShape(QFrame.StyledPanel)
        hb = QHBoxLayout(row); hb.setContentsMargins(4, 4, 4, 4); hb.setSpacing(4)

        up = QPushButton(); up.setFixedWidth(24)
        up.setIcon(icon_rotated("arrow.png", 180, 12, "#ffffff"))
        up.setToolTip(self.tr("Move up"))
        down = QPushButton(); down.setFixedWidth(24)
        down.setIcon(icon_rotated("arrow.png", 0, 12, "#ffffff"))
        down.setToolTip(self.tr("Move down"))

        path_edit = self._mono_edit(self.tr("e.g. output/extensions"))
        type_combo = QComboBox()
        for _val, _lbl in (("folders", self.tr("folders")),
                           ("filenames", self.tr("filenames")),
                           ("extensions", self.tr("extensions"))):
            type_combo.addItem(_lbl, userData=_val)
        _ti = type_combo.findData(match_type)
        type_combo.setCurrentIndex(_ti if _ti >= 0 else 0)
        type_combo.setFixedWidth(self._TYPE_COMBO_W)
        no_wheel(type_combo)
        value_edit = self._mono_edit(self.tr("File/Folder"))
        path_edit.setText(path)
        value_edit.setText(match_value)

        remove = QPushButton(); remove.setObjectName("DangerButton")
        remove.setIcon(icon("close_white.png", 12))
        remove.setToolTip(self.tr("Remove rule"))
        remove.setFixedWidth(28)

        vbtns = QVBoxLayout(); vbtns.setSpacing(0); vbtns.setContentsMargins(0, 0, 0, 0)
        vbtns.addWidget(up); vbtns.addWidget(down)
        hb.addLayout(vbtns)
        hb.addWidget(path_edit, 1)
        hb.addWidget(type_combo)
        hb.addWidget(value_edit, 1)
        hb.addWidget(remove)

        rd = {"frame": row, "path": path_edit, "type": type_combo,
              "value": value_edit}
        self._whitelist_rows.append(rd)
        self._whitelist_vbox.addWidget(row)
        self._whitelist_header.setVisible(True)

        up.clicked.connect(lambda: self._move_whitelist_rule(rd, -1))
        down.clicked.connect(lambda: self._move_whitelist_rule(rd, 1))
        remove.clicked.connect(lambda: self._remove_whitelist_rule(rd))

    def _remove_whitelist_rule(self, rd):
        if rd in self._whitelist_rows:
            self._whitelist_rows.remove(rd)
            self._whitelist_vbox.removeWidget(rd["frame"])
            rd["frame"].deleteLater()
            if not self._whitelist_rows:
                self._whitelist_header.setVisible(False)

    def _move_whitelist_rule(self, rd, delta):
        rows = self._whitelist_rows
        if rd not in rows:
            return
        i = rows.index(rd)
        j = i + delta
        if j < 0 or j >= len(rows):
            return
        rows[i], rows[j] = rows[j], rows[i]
        self._whitelist_vbox.removeWidget(rd["frame"])
        self._whitelist_vbox.insertWidget(j, rd["frame"])

    def _collect_whitelist_rules(self) -> list[dict]:
        rules = []
        for rd in self._whitelist_rows:
            path = rd["path"].text().strip()
            match_type = rd["type"].currentData()
            values = [v.strip() for v in rd["value"].text().split(",") if v.strip()]
            if not values:
                continue          # empty path means game root, but need a value
            rule: dict = {"path": path}
            if match_type == "extensions":
                rule["extensions"] = values
            elif match_type == "filenames":
                rule["filenames"] = values
            else:
                rule["folders"] = values
            rules.append(rule)
        return rules

    # ---- framework rows ---------------------------------------------------
    def _add_framework(self, name="", path=""):
        row = QFrame(); row.setObjectName("FwRow")
        row.setFrameShape(QFrame.StyledPanel)
        hb = QHBoxLayout(row); hb.setContentsMargins(4, 4, 4, 4); hb.setSpacing(4)
        name_edit = self._mono_edit(self.tr("e.g. Script Extender"))
        path_edit = self._mono_edit(self.tr("e.g. skse64_loader.exe"))
        name_edit.setText(name); path_edit.setText(path)
        remove = QPushButton(); remove.setObjectName("DangerButton")
        remove.setIcon(icon("close_white.png", 12))
        remove.setToolTip(self.tr("Remove framework"))
        remove.setFixedWidth(28)
        hb.addWidget(name_edit, 1)
        hb.addWidget(path_edit, 2)
        hb.addWidget(remove)

        rd = {"frame": row, "name": name_edit, "path": path_edit}
        self._framework_rows.append(rd)
        self._framework_vbox.addWidget(row)
        remove.clicked.connect(lambda: self._remove_framework(rd))

    def _remove_framework(self, rd):
        if rd in self._framework_rows:
            self._framework_rows.remove(rd)
            self._framework_vbox.removeWidget(rd["frame"])
            rd["frame"].deleteLater()

    def _collect_frameworks(self) -> dict[str, str]:
        result: dict[str, str] = {}
        for rd in self._framework_rows:
            name = rd["name"].text().strip()
            path = rd["path"].text().strip()
            if name and path:
                result[name] = path
        return result

    # ---- preset dropdown (create mode) ------------------------------------
    def _on_preset_selected(self, _idx: int):
        defn = self._preset_combo.currentData()
        if not defn:
            return
        # Clear any dynamic-table rows from a previous preset selection so the
        # form reflects only the newly chosen template.
        for rd in list(self._routing_rows):
            self._remove_routing_rule(rd)
        for rd in list(self._whitelist_rows):
            self._remove_whitelist_rule(rd)
        for rd in list(self._framework_rows):
            self._remove_framework(rd)
        # Populate every field from the preset, but leave the name blank — the
        # new game needs its own unique name.
        self._prepopulate(defn, keep_name=False)
        self._update_data_path_visibility()

    # ---- share code export / import ---------------------------------------
    def _on_export_code(self):
        """Export the current game's settings as a copy-pasteable share code."""
        err = self._validate()
        if err:
            self._validation.setText(err)
            return
        self._validation.setText("")
        from gui_qt.share_code_overlay import CustomGameExportOverlay
        defn = self._collect_definition()
        code = encode_custom_game_definition(defn)
        CustomGameExportOverlay(self.window(), code, game_name=defn.get("name", ""))

    def _on_import_code(self):
        """Prefill the (define-mode) form from a pasted share code."""
        from gui_qt.share_code_overlay import CustomGameImportOverlay

        def _done(defn):
            if not defn:
                return
            # Clear any dynamic-table rows before applying the imported preset.
            for rd in list(self._routing_rows):
                self._remove_routing_rule(rd)
            for rd in list(self._whitelist_rows):
                self._remove_whitelist_rule(rd)
            for rd in list(self._framework_rows):
                self._remove_framework(rd)
            # Keep the imported name as a starting point; the user can rename it.
            self._prepopulate(defn, keep_name=True)
            self._update_data_path_visibility()
            if hasattr(self, "_preset_combo"):
                self._preset_combo.blockSignals(True)
                self._preset_combo.setCurrentIndex(0)
                self._preset_combo.blockSignals(False)

        CustomGameImportOverlay(self.window(), _done)

    # ---- prepopulate (edit mode / preset load) ----------------------------
    def _prepopulate(self, defn: dict | None = None, keep_name: bool = True):
        e = defn if defn is not None else self._existing
        if not e:
            return
        if keep_name:
            self._name_edit.setText(e.get("name", ""))
        self._exe_edit.setText(e.get("exe_name", ""))
        dep = e.get("deploy_type", "standard")
        if dep in self._deploy_buttons:
            self._deploy_buttons[dep].setChecked(True)
        self._data_path_edit.setText(e.get("mod_data_path", ""))
        self._steam_edit.setText(e.get("steam_id", ""))
        self._nexus_edit.setText(e.get("nexus_game_domain", ""))
        self._image_edit.setText(e.get("image_url", ""))

        self._adv_edits["mod_folder_strip_prefixes"].setText(
            _set_to_str(e.get("mod_folder_strip_prefixes", [])))
        self._adv_edits["mod_install_prefix"].setText(e.get("mod_install_prefix", ""))
        self._adv_edits["mod_required_top_level_folders"].setText(
            _set_to_str(e.get("mod_required_top_level_folders", [])))
        self._adv_edits["mod_required_file_types"].setText(
            _set_to_str(e.get("mod_required_file_types", [])))
        self._adv_edits["mod_folder_strip_prefixes_post"].setText(
            _set_to_str(e.get("mod_folder_strip_prefixes_post", [])))
        self._adv_edits["conflict_ignore_filenames"].setText(
            _set_to_str(e.get("conflict_ignore_filenames", [])))
        self._adv_edits["conflict_ignore_foldernames"].setText(
            _set_to_str(e.get("conflict_ignore_foldernames", [])))

        self._adv_toggles["mod_auto_strip_until_required"].setChecked(
            bool(e.get("mod_auto_strip_until_required", False)))
        self._adv_toggles["mod_install_as_is_if_no_match"].setChecked(
            bool(e.get("mod_install_as_is_if_no_match", False)))
        self._adv_toggles["restore_before_deploy"].setChecked(
            bool(e.get("restore_before_deploy", True)))
        self._adv_toggles["normalize_folder_case"].setChecked(
            bool(e.get("normalize_folder_case", True)))

        casing_idx = self._casing_combo.findData(
            e.get("filemap_casing", "upper"))
        self._casing_combo.setCurrentIndex(casing_idx if casing_idx >= 0 else 0)

        self._dll_edit.setPlainText(_dll_to_str(e.get("wine_dll_overrides", {})))

        for rule in e.get("custom_routing_rules", []) or []:
            if not isinstance(rule, dict):
                continue
            companions = rule.get("companion_extensions") or []
            if rule.get("filenames"):
                mt, mv = "filenames", ", ".join(rule["filenames"])
            elif rule.get("extensions"):
                mt = "extensions"
                mv = ", ".join(rule["extensions"])
                if companions:
                    mv = f"{mv} ({', '.join(companions)})"
            else:
                mt, mv = "folders", ", ".join(rule.get("folders") or [])
            self._add_routing_rule(
                dest=rule.get("dest", ""), match_type=mt, match_value=mv,
                loose_only=bool(rule.get("loose_only", False)),
                flatten=bool(rule.get("flatten", False)),
                include_siblings=bool(rule.get("include_siblings", False)),
                to_prefix=bool(rule.get("to_prefix", False)))

        for rule in e.get("restore_whitelist", []) or []:
            if not isinstance(rule, dict):
                continue
            if rule.get("filenames"):
                mt, mv = "filenames", ", ".join(rule["filenames"])
            elif rule.get("extensions"):
                mt, mv = "extensions", ", ".join(rule["extensions"])
            else:
                mt, mv = "folders", ", ".join(rule.get("folders") or [])
            self._add_whitelist_rule(
                path=rule.get("path", ""), match_type=mt, match_value=mv)

        fw = e.get("custom_frameworks", {})
        if isinstance(fw, dict):
            for name, path in fw.items():
                self._add_framework(name=name, path=path)

        self._auto_expand_sections()

    def _auto_expand_sections(self):
        """Expand collapsed sections that hold non-default content, so nothing
        a prepopulate filled in (edit mode, preset, share-code import) is
        hidden. Expand-only — never re-collapse a section the user opened."""
        edits = self._adv_edits
        toggles = self._adv_toggles
        tuning_keys = (
            "mod_folder_strip_prefixes", "mod_install_prefix",
            "mod_required_top_level_folders", "mod_required_file_types",
            "mod_folder_strip_prefixes_post",
        )
        if (any(edits[k].text().strip() for k in tuning_keys)
                or toggles["mod_auto_strip_until_required"].isChecked()
                or toggles["mod_install_as_is_if_no_match"].isChecked()):
            self._sec_tuning.expand()
        if (edits["conflict_ignore_filenames"].text().strip()
                or edits["conflict_ignore_foldernames"].text().strip()
                or not toggles["restore_before_deploy"].isChecked()
                or not toggles["normalize_folder_case"].isChecked()
                or (self._casing_combo.currentData() or "upper") != "upper"
                or self._dll_edit.toPlainText().strip()):
            self._sec_conflicts.expand()
        if self._routing_rows:
            self._sec_routing.expand()
        if self._whitelist_rows:
            self._sec_whitelist.expand()
        if self._framework_rows:
            self._sec_frameworks.expand()

    # ---- validate ---------------------------------------------------------
    def _validate(self) -> str | None:
        name = self._name_edit.text().strip()
        exe = self._exe_edit.text().strip()
        if not name:
            return "Game Name is required."
        if not exe:
            return "Executable Filename is required."
        if len(name) > 120:
            return "Game Name is too long (max 120 characters)."
        return None

    # ---- banner image download (worker → signal) -------------------------
    def _set_image_status(self, text, tone):
        self._image_status.setText(text)
        self._image_status.setStyleSheet(f"color:{self._c(tone)};")

    def _download_image(self, url: str, game_id: str):
        def _worker():
            try:
                import requests
                from PIL import Image as PilImage
                safe_emit(self._img_sig.status, self.tr("Downloading image…"), "TEXT_WARN")
                resp = requests.get(url, timeout=15)
                resp.raise_for_status()
                img = PilImage.open(io.BytesIO(resp.content)).convert("RGBA")
                out = get_custom_game_images_dir() / f"{game_id}.png"
                img.save(out, "PNG")
                safe_emit(self._img_sig.status, self.tr("Image cached."), "TEXT_OK")
            except Exception as exc:
                safe_emit(self._img_sig.status,
                          self.tr("Image download failed: {0}").format(exc), "TEXT_ERR")

        threading.Thread(target=_worker, daemon=True).start()

    # ---- save / delete / cancel -------------------------------------------
    def _collect_definition(self) -> dict:
        """Build a custom game definition dict from the current form state.

        Shared by Save and Export so the exported code is exactly what would be
        saved. Callers should run :meth:`_validate` first.
        """
        name = self._name_edit.text().strip()
        exe = self._exe_edit.text().strip()
        deploy = self._current_deploy_type()
        data_path = (self._data_path_edit.text().strip()
                     if deploy in ("standard", "ue5") else "")
        image_url = self._image_edit.text().strip()

        game_id = (self._existing.get("game_id") if self._existing else None) \
            or _make_game_id(name)

        defn = {
            "name":              name,
            "game_id":           game_id,
            "exe_name":          exe,
            "deploy_type":       deploy,
            "mod_data_path":     data_path,
            "steam_id":          self._steam_edit.text().strip(),
            "nexus_game_domain": self._nexus_edit.text().strip(),
            "image_url":         image_url,
            "mod_folder_strip_prefixes":
                _str_to_list(self._adv_edits["mod_folder_strip_prefixes"].text()),
            "conflict_ignore_filenames":
                _str_to_list(self._adv_edits["conflict_ignore_filenames"].text()),
            "conflict_ignore_foldernames":
                _str_to_list(self._adv_edits["conflict_ignore_foldernames"].text()),
            "mod_folder_strip_prefixes_post":
                _str_to_list(self._adv_edits["mod_folder_strip_prefixes_post"].text()),
            "mod_install_prefix":
                self._adv_edits["mod_install_prefix"].text().strip(),
            "mod_required_top_level_folders":
                _str_to_list(self._adv_edits["mod_required_top_level_folders"].text()),
            "mod_auto_strip_until_required":
                self._adv_toggles["mod_auto_strip_until_required"].isChecked(),
            "mod_required_file_types":
                _str_to_list(self._adv_edits["mod_required_file_types"].text()),
            "mod_install_as_is_if_no_match":
                self._adv_toggles["mod_install_as_is_if_no_match"].isChecked(),
            "restore_before_deploy":
                self._adv_toggles["restore_before_deploy"].isChecked(),
            "normalize_folder_case":
                self._adv_toggles["normalize_folder_case"].isChecked(),
            "filemap_casing":
                self._casing_combo.currentData() or "upper",
            "wine_dll_overrides": _parse_dll_text(self._dll_edit.toPlainText()),
            "custom_routing_rules": self._collect_routing_rules(),
            "restore_whitelist": self._collect_whitelist_rules(),
            "custom_frameworks": self._collect_frameworks(),
        }

        # Preserve repo-handler metadata when editing.
        if self._existing:
            for key in ("version", "editable"):
                if key in self._existing:
                    defn[key] = self._existing[key]
        return defn

    def _on_save(self):
        err = self._validate()
        if err:
            self._validation.setText(err)
            return
        self._validation.setText("")

        defn = self._collect_definition()
        image_url = defn.get("image_url", "")
        game_id = defn.get("game_id", "")

        save_custom_game_definition(defn)
        # Materialise the handler so any errors surface here, not later.
        make_custom_game(defn)

        if image_url:
            self._download_image(image_url, game_id)

        self._on_done(defn, False)

    def _on_delete(self):
        if self._existing is None:
            return
        game_id = self._existing.get("game_id", "")
        if game_id:
            delete_custom_game_definition(game_id)
            img = get_custom_game_images_dir() / f"{game_id}.png"
            img.unlink(missing_ok=True)
        self._on_done(None, True)
