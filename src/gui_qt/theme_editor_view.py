"""Theme editor - a full-screen tab for authoring custom colour themes.

Opened from Settings ▸ User Interface ("Edit / Create Theme…") via
``app._open_theme_editor_tab`` → ``DetachableTabWidget.open_tab(..., key=
"theme_editor")``. The user picks a "Start from" theme (any built-in or existing
custom theme), edits colours grouped by role, and saves the result as a JSON
theme in ``<config>/themes/`` (see ``Utils.custom_themes``). Saving selects the
new theme as the active ``appearance_mode``.

Grouping + derivation come from ``theme_editor_groups``. The default view is a
compact semantic palette; editing a colour also updates equivalent roles and
hover variants. "Fine tune" reveals the implemented app-specific roles and
unlocks each one individually.

The working palette is applied to the running Qt application after a source
theme is loaded or a colour is confirmed. It remains temporary until Save/Save
As persists it; permanently closing the editor restores the persisted theme.
The sandbox preview stays useful while this full-screen tab hides most of the
real application.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, QT_TRANSLATE_NOOP, QTimer
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QScrollArea, QFrame,
    QLabel, QComboBox, QPushButton, QCheckBox, QGroupBox, QSplitter,
    QApplication,
)

from gui_qt.theme_qt import active_palette, apply_theme, _c
from gui_qt.theme_preview import ThemePreviewPanel
from gui_qt.color_picker_overlay import ColorPickerOverlay
from gui_qt.confirm_overlay import ConfirmOverlay
from gui_qt.wheel_guard import no_wheel
from gui_qt import theme_editor_groups as teg
from Utils.themes import load_palettes, load_display_names, get_ctk_appearance
from Utils import custom_themes as ct
from Utils import ui_config as uc


# lupdate extraction anchors: the theme-editor section titles, swatch labels
# and section descriptions all live as plain strings in theme_editor_groups
# (a Qt-free data module). Marking the literals here puts them in the
# ThemeEditorView context so the self.tr() calls in _rebuild_body() resolve
# them at render time. Regenerate with tools/i18n/i18n_update.sh after editing
# GROUPS / GROUP_DESCRIPTIONS.
_TR_MARKERS = (
    QT_TRANSLATE_NOOP("ThemeEditorView", "Backgrounds"),
    QT_TRANSLATE_NOOP("ThemeEditorView", "App background (deepest)"),
    QT_TRANSLATE_NOOP("ThemeEditorView", "Panel / card surface"),
    QT_TRANSLATE_NOOP("ThemeEditorView", "Header / toolbar"),
    QT_TRANSLATE_NOOP("ThemeEditorView", "List row"),
    QT_TRANSLATE_NOOP("ThemeEditorView", "List row (alt stripe)"),
    QT_TRANSLATE_NOOP("ThemeEditorView", "List row hover"),
    QT_TRANSLATE_NOOP("ThemeEditorView", "Tree / list surface"),
    QT_TRANSLATE_NOOP("ThemeEditorView", "Separator fill"),
    QT_TRANSLATE_NOOP("ThemeEditorView", "Hover highlight"),
    QT_TRANSLATE_NOOP("ThemeEditorView", "Selection highlight"),
    QT_TRANSLATE_NOOP("ThemeEditorView", "Text input field"),
    QT_TRANSLATE_NOOP("ThemeEditorView", "Text"),
    QT_TRANSLATE_NOOP("ThemeEditorView", "Primary text"),
    QT_TRANSLATE_NOOP("ThemeEditorView", "Dimmed text"),
    QT_TRANSLATE_NOOP("ThemeEditorView", "Muted text"),
    QT_TRANSLATE_NOOP("ThemeEditorView", "Faint text"),
    QT_TRANSLATE_NOOP("ThemeEditorView", "Separator text"),
    QT_TRANSLATE_NOOP("ThemeEditorView", "White"),
    QT_TRANSLATE_NOOP("ThemeEditorView", "Black"),
    QT_TRANSLATE_NOOP("ThemeEditorView", "Success text"),
    QT_TRANSLATE_NOOP("ThemeEditorView", "Error text"),
    QT_TRANSLATE_NOOP("ThemeEditorView", "Warning text"),
    QT_TRANSLATE_NOOP("ThemeEditorView", "Success text (bright)"),
    QT_TRANSLATE_NOOP("ThemeEditorView", "Error text (bright)"),
    QT_TRANSLATE_NOOP("ThemeEditorView", "Warning text (bright)"),
    QT_TRANSLATE_NOOP("ThemeEditorView", "Card text"),
    QT_TRANSLATE_NOOP("ThemeEditorView", "Card text (dim)"),
    QT_TRANSLATE_NOOP("ThemeEditorView", "Card text (medium)"),
    QT_TRANSLATE_NOOP("ThemeEditorView", "Tree foreground"),
    QT_TRANSLATE_NOOP("ThemeEditorView", "Accent"),
    QT_TRANSLATE_NOOP("ThemeEditorView", "Accent hover"),
    QT_TRANSLATE_NOOP("ThemeEditorView", "Text on accent"),
    QT_TRANSLATE_NOOP("ThemeEditorView", "Hyperlink"),
    QT_TRANSLATE_NOOP("ThemeEditorView", "Dropdown arrow"),
    QT_TRANSLATE_NOOP("ThemeEditorView", "Borders"),
    QT_TRANSLATE_NOOP("ThemeEditorView", "Border"),
    QT_TRANSLATE_NOOP("ThemeEditorView", "Border (dim)"),
    QT_TRANSLATE_NOOP("ThemeEditorView", "Border (faint)"),
    QT_TRANSLATE_NOOP("ThemeEditorView", "Buttons - Red"),
    QT_TRANSLATE_NOOP("ThemeEditorView", "Danger"),
    QT_TRANSLATE_NOOP("ThemeEditorView", "Danger hover"),
    QT_TRANSLATE_NOOP("ThemeEditorView", "Danger (alt)"),
    QT_TRANSLATE_NOOP("ThemeEditorView", "Danger alt hover"),
    QT_TRANSLATE_NOOP("ThemeEditorView", "Danger (deep)"),
    QT_TRANSLATE_NOOP("ThemeEditorView", "Danger deep hover"),
    QT_TRANSLATE_NOOP("ThemeEditorView", "Cancel"),
    QT_TRANSLATE_NOOP("ThemeEditorView", "Cancel hover"),
    QT_TRANSLATE_NOOP("ThemeEditorView", "Red (legacy)"),
    QT_TRANSLATE_NOOP("ThemeEditorView", "Red hover (legacy)"),
    QT_TRANSLATE_NOOP("ThemeEditorView", "Buttons - Green"),
    QT_TRANSLATE_NOOP("ThemeEditorView", "Success"),
    QT_TRANSLATE_NOOP("ThemeEditorView", "Success hover"),
    QT_TRANSLATE_NOOP("ThemeEditorView", "Success (alt)"),
    QT_TRANSLATE_NOOP("ThemeEditorView", "Success alt hover"),
    QT_TRANSLATE_NOOP("ThemeEditorView", "Success (deep)"),
    QT_TRANSLATE_NOOP("ThemeEditorView", "Success deep hover"),
    QT_TRANSLATE_NOOP("ThemeEditorView", "Buttons - Orange"),
    QT_TRANSLATE_NOOP("ThemeEditorView", "Warning"),
    QT_TRANSLATE_NOOP("ThemeEditorView", "Warning hover"),
    QT_TRANSLATE_NOOP("ThemeEditorView", "Warning (deep)"),
    QT_TRANSLATE_NOOP("ThemeEditorView", "Warning deep hover"),
    QT_TRANSLATE_NOOP("ThemeEditorView", "Warning (brown)"),
    QT_TRANSLATE_NOOP("ThemeEditorView", "Warning brown hover"),
    QT_TRANSLATE_NOOP("ThemeEditorView", "Warning (orange)"),
    QT_TRANSLATE_NOOP("ThemeEditorView", "Warning orange hover"),
    QT_TRANSLATE_NOOP("ThemeEditorView", "Buttons - Blue"),
    QT_TRANSLATE_NOOP("ThemeEditorView", "Info"),
    QT_TRANSLATE_NOOP("ThemeEditorView", "Info hover"),
    QT_TRANSLATE_NOOP("ThemeEditorView", "Info (deep)"),
    QT_TRANSLATE_NOOP("ThemeEditorView", "Info deep hover"),
    QT_TRANSLATE_NOOP("ThemeEditorView", "Neutral"),
    QT_TRANSLATE_NOOP("ThemeEditorView", "Neutral hover"),
    QT_TRANSLATE_NOOP("ThemeEditorView", "Buttons - Grey"),
    QT_TRANSLATE_NOOP("ThemeEditorView", "Grey"),
    QT_TRANSLATE_NOOP("ThemeEditorView", "Grey hover"),
    QT_TRANSLATE_NOOP("ThemeEditorView", "Grey (alt)"),
    QT_TRANSLATE_NOOP("ThemeEditorView", "Grey alt hover"),
    QT_TRANSLATE_NOOP("ThemeEditorView", "Buttons - Purple"),
    QT_TRANSLATE_NOOP("ThemeEditorView", "Purple"),
    QT_TRANSLATE_NOOP("ThemeEditorView", "Purple hover"),
    QT_TRANSLATE_NOOP("ThemeEditorView", "Tree tags"),
    QT_TRANSLATE_NOOP("ThemeEditorView", "Folder"),
    QT_TRANSLATE_NOOP("ThemeEditorView", "BSA archive"),
    QT_TRANSLATE_NOOP("ThemeEditorView", "BSA archive (alt)"),
    QT_TRANSLATE_NOOP("ThemeEditorView", "INI profile"),
    QT_TRANSLATE_NOOP("ThemeEditorView", "Bundled (text)"),
    QT_TRANSLATE_NOOP("ThemeEditorView", "Bundled (background)"),
    QT_TRANSLATE_NOOP("ThemeEditorView", "Installed (background)"),
    QT_TRANSLATE_NOOP("ThemeEditorView", "Unordered (text)"),
    QT_TRANSLATE_NOOP("ThemeEditorView", "Tones"),
    QT_TRANSLATE_NOOP("ThemeEditorView", "Green tone"),
    QT_TRANSLATE_NOOP("ThemeEditorView", "Red tone"),
    QT_TRANSLATE_NOOP("ThemeEditorView", "Blue tone"),
    QT_TRANSLATE_NOOP("ThemeEditorView", "Cyan tone"),
    QT_TRANSLATE_NOOP("ThemeEditorView", "Soft blue tone"),
    QT_TRANSLATE_NOOP("ThemeEditorView", "Flag tone"),
    QT_TRANSLATE_NOOP("ThemeEditorView", "Scrollbars"),
    QT_TRANSLATE_NOOP("ThemeEditorView", "Scrollbar background"),
    QT_TRANSLATE_NOOP("ThemeEditorView", "Scrollbar trough"),
    QT_TRANSLATE_NOOP("ThemeEditorView", "Scrollbar thumb (active)"),
    QT_TRANSLATE_NOOP("ThemeEditorView", "Overlays & tinted rows"),
    QT_TRANSLATE_NOOP("ThemeEditorView", "Error overlay"),
    QT_TRANSLATE_NOOP("ThemeEditorView", "Deep overlay"),
    QT_TRANSLATE_NOOP("ThemeEditorView", "Card"),
    QT_TRANSLATE_NOOP("ThemeEditorView", "Card (alt)"),
    QT_TRANSLATE_NOOP("ThemeEditorView", "Green row"),
    QT_TRANSLATE_NOOP("ThemeEditorView", "Green (deep)"),
    QT_TRANSLATE_NOOP("ThemeEditorView", "Red (deep)"),
    QT_TRANSLATE_NOOP("ThemeEditorView", "Orange (deep)"),
    QT_TRANSLATE_NOOP("ThemeEditorView", "Blue (deep)"),
    QT_TRANSLATE_NOOP("ThemeEditorView", "Green tint text"),
    QT_TRANSLATE_NOOP("ThemeEditorView", "Red tint text"),
    QT_TRANSLATE_NOOP("ThemeEditorView", "Orange tint text"),
    QT_TRANSLATE_NOOP("ThemeEditorView", "Blue tint text"),
    QT_TRANSLATE_NOOP("ThemeEditorView", "Dark blue"),
    QT_TRANSLATE_NOOP("ThemeEditorView", "Dark green"),
    QT_TRANSLATE_NOOP("ThemeEditorView", "Save button"),
    QT_TRANSLATE_NOOP("ThemeEditorView", "Selection bar"),
    QT_TRANSLATE_NOOP("ThemeEditorView", "Required mod"),
    QT_TRANSLATE_NOOP("ThemeEditorView", "Optional mod"),
    QT_TRANSLATE_NOOP("ThemeEditorView", "Status"),
    QT_TRANSLATE_NOOP("ThemeEditorView", "Error (bright)"),
    QT_TRANSLATE_NOOP("ThemeEditorView", "Badge red"),
    QT_TRANSLATE_NOOP("ThemeEditorView", "Badge green"),
    QT_TRANSLATE_NOOP("ThemeEditorView", "Success (solid)"),
    QT_TRANSLATE_NOOP("ThemeEditorView", "Queued"),
    QT_TRANSLATE_NOOP("ThemeEditorView", "Download green"),
    QT_TRANSLATE_NOOP("ThemeEditorView", "Plugin cycle & files"),
    QT_TRANSLATE_NOOP("ThemeEditorView", "Cycle error row (bg)"),
    QT_TRANSLATE_NOOP("ThemeEditorView", "Cycle error row (text)"),
    QT_TRANSLATE_NOOP("ThemeEditorView", "Cycle ok row (bg)"),
    QT_TRANSLATE_NOOP("ThemeEditorView", "Cycle ok row (text)"),
    QT_TRANSLATE_NOOP("ThemeEditorView", "Cycle warn row (bg)"),
    QT_TRANSLATE_NOOP("ThemeEditorView", "Cycle warn row (text)"),
    QT_TRANSLATE_NOOP("ThemeEditorView", "Cycle anchor"),
    QT_TRANSLATE_NOOP("ThemeEditorView", "Cycle link"),
    QT_TRANSLATE_NOOP("ThemeEditorView", "File winning"),
    QT_TRANSLATE_NOOP("ThemeEditorView", "File overridden"),
    QT_TRANSLATE_NOOP("ThemeEditorView", "File dim"),
    QT_TRANSLATE_NOOP("ThemeEditorView", "File anchor"),
    QT_TRANSLATE_NOOP("ThemeEditorView", "Drag selection outline"),
    QT_TRANSLATE_NOOP("ThemeEditorView", "Conflict highlights"),
    QT_TRANSLATE_NOOP("ThemeEditorView", "Conflict row - winning"),
    QT_TRANSLATE_NOOP("ThemeEditorView", "Conflict row - overridden"),
    QT_TRANSLATE_NOOP("ThemeEditorView", "Conflict row - anchor"),
    QT_TRANSLATE_NOOP("ThemeEditorView", "Framework detection"),
    QT_TRANSLATE_NOOP("ThemeEditorView", "Installed (bg)"),
    QT_TRANSLATE_NOOP("ThemeEditorView", "Installed (text)"),
    QT_TRANSLATE_NOOP("ThemeEditorView", "Staged (bg)"),
    QT_TRANSLATE_NOOP("ThemeEditorView", "Staged (text)"),
    QT_TRANSLATE_NOOP("ThemeEditorView", "Disabled (bg)"),
    QT_TRANSLATE_NOOP("ThemeEditorView", "Disabled (text)"),
    QT_TRANSLATE_NOOP("ThemeEditorView", "Missing (bg)"),
    QT_TRANSLATE_NOOP("ThemeEditorView", "Missing (text)"),
    QT_TRANSLATE_NOOP("ThemeEditorView", "Separator bands"),
    QT_TRANSLATE_NOOP("ThemeEditorView", "Overwrite band (bg)"),
    QT_TRANSLATE_NOOP("ThemeEditorView", "Overwrite band (text)"),
    QT_TRANSLATE_NOOP("ThemeEditorView", "Root Folder band (bg)"),
    QT_TRANSLATE_NOOP("ThemeEditorView", "Root Folder band (text)"),
    QT_TRANSLATE_NOOP("ThemeEditorView", "Checkboxes"),
    QT_TRANSLATE_NOOP("ThemeEditorView", "Checkbox fill (checked)"),
    QT_TRANSLATE_NOOP("ThemeEditorView", "Surfaces"),
    QT_TRANSLATE_NOOP("ThemeEditorView", "Window background"),
    QT_TRANSLATE_NOOP("ThemeEditorView", "Panels and dialogs"),
    QT_TRANSLATE_NOOP("ThemeEditorView", "Toolbars and headers"),
    QT_TRANSLATE_NOOP("ThemeEditorView", "List / tree background"),
    QT_TRANSLATE_NOOP("ThemeEditorView", "List row background"),
    QT_TRANSLATE_NOOP("ThemeEditorView", "Secondary text"),
    QT_TRANSLATE_NOOP("ThemeEditorView", "Accent and selection"),
    QT_TRANSLATE_NOOP("ThemeEditorView", "Accent, links and controls"),
    QT_TRANSLATE_NOOP("ThemeEditorView", "Selected rows"),
    QT_TRANSLATE_NOOP("ThemeEditorView", "Borders and dividers"),
    QT_TRANSLATE_NOOP("ThemeEditorView", "Action buttons"),
    QT_TRANSLATE_NOOP("ThemeEditorView", "Confirm / install"),
    QT_TRANSLATE_NOOP("ThemeEditorView", "Delete / remove"),
    QT_TRANSLATE_NOOP("ThemeEditorView", "Warning / update"),
    QT_TRANSLATE_NOOP("ThemeEditorView", "Info / select"),
    QT_TRANSLATE_NOOP("ThemeEditorView", "Secondary action"),
    QT_TRANSLATE_NOOP("ThemeEditorView", "The main layers of the app, from the window to list rows."),
    QT_TRANSLATE_NOOP("ThemeEditorView", "General text plus the three semantic status colours."),
    QT_TRANSLATE_NOOP("ThemeEditorView", "Brand colour, selected rows, focus controls and dividers."),
    QT_TRANSLATE_NOOP("ThemeEditorView", "Button colours are shared by actions with the same meaning."),
    QT_TRANSLATE_NOOP("ThemeEditorView", "Surfaces and rows"),
    QT_TRANSLATE_NOOP("ThemeEditorView", "Status text"),
    QT_TRANSLATE_NOOP("ThemeEditorView", "Accent and links"),
    QT_TRANSLATE_NOOP("ThemeEditorView", "Selection and focus"),
    QT_TRANSLATE_NOOP("ThemeEditorView", "Borders and separators"),
    QT_TRANSLATE_NOOP("ThemeEditorView", "Danger buttons"),
    QT_TRANSLATE_NOOP("ThemeEditorView", "Success buttons"),
    QT_TRANSLATE_NOOP("ThemeEditorView", "Warning buttons"),
    QT_TRANSLATE_NOOP("ThemeEditorView", "Information buttons"),
    QT_TRANSLATE_NOOP("ThemeEditorView", "Secondary buttons"),
    QT_TRANSLATE_NOOP("ThemeEditorView", "Special accent buttons"),
    QT_TRANSLATE_NOOP("ThemeEditorView", "Scrollbars and checkboxes"),
    QT_TRANSLATE_NOOP("ThemeEditorView", "Icons and small highlights"),
    QT_TRANSLATE_NOOP("ThemeEditorView", "Tinted content rows"),
    QT_TRANSLATE_NOOP("ThemeEditorView", "Required and optional mods"),
    QT_TRANSLATE_NOOP("ThemeEditorView", "Notifications and queues"),
    QT_TRANSLATE_NOOP("ThemeEditorView", "Plugin cycle"),
    QT_TRANSLATE_NOOP("ThemeEditorView", "File conflicts"),
    QT_TRANSLATE_NOOP("ThemeEditorView", "Conflict and requirement highlights"),
    QT_TRANSLATE_NOOP("ThemeEditorView", "Framework status"),
    QT_TRANSLATE_NOOP("ThemeEditorView", "Mod list separator bands"),
    QT_TRANSLATE_NOOP("ThemeEditorView", "Alternate list row"),
    QT_TRANSLATE_NOOP("ThemeEditorView", "Hovered list row"),
    QT_TRANSLATE_NOOP("ThemeEditorView", "Card background"),
    QT_TRANSLATE_NOOP("ThemeEditorView", "Text on accent / selection"),
    QT_TRANSLATE_NOOP("ThemeEditorView", "Separator row background"),
    QT_TRANSLATE_NOOP("ThemeEditorView", "Separator row text"),
    QT_TRANSLATE_NOOP("ThemeEditorView", "Window, panel, card, list and row backgrounds."),
    QT_TRANSLATE_NOOP("ThemeEditorView", "Primary, secondary and faint text used throughout the app."),
    QT_TRANSLATE_NOOP("ThemeEditorView", "Success, warning and error messages shown on neutral backgrounds."),
    QT_TRANSLATE_NOOP("ThemeEditorView", "Brand accent, contrasting text, hyperlinks and control glyphs."),
    QT_TRANSLATE_NOOP("ThemeEditorView", "Hover, selected-row and drag-selection colours."),
    QT_TRANSLATE_NOOP("ThemeEditorView", "Frames, divider lines and separator rows."),
    QT_TRANSLATE_NOOP("ThemeEditorView", "Delete, remove and other destructive actions."),
    QT_TRANSLATE_NOOP("ThemeEditorView", "Install, confirm, Done and Play actions."),
    QT_TRANSLATE_NOOP("ThemeEditorView", "Update, reinstall and cautionary actions."),
    QT_TRANSLATE_NOOP("ThemeEditorView", "Select, Groups, Plugin Rules and similar actions."),
    QT_TRANSLATE_NOOP("ThemeEditorView", "View and other low-emphasis actions."),
    QT_TRANSLATE_NOOP("ThemeEditorView", "Special-purpose accent buttons such as Ko-Fi."),
    QT_TRANSLATE_NOOP("ThemeEditorView", "Scrollbar track/thumb and checked-box fill."),
    QT_TRANSLATE_NOOP("ThemeEditorView", "Shared tones used by icons, flags and file-tree markers."),
    QT_TRANSLATE_NOOP("ThemeEditorView", "Coloured information rows and their foreground text."),
    QT_TRANSLATE_NOOP("ThemeEditorView", "Required/optional indicators in collection views."),
    QT_TRANSLATE_NOOP("ThemeEditorView", "Error badges, notifications and queued states."),
    QT_TRANSLATE_NOOP("ThemeEditorView", "Cycle status rows and before/after rule keywords."),
    QT_TRANSLATE_NOOP("ThemeEditorView", "Winning, overridden, inactive and anchor files."),
    QT_TRANSLATE_NOOP("ThemeEditorView", "Related mod rows highlighted across Mods, Plugins and Data."),
    QT_TRANSLATE_NOOP("ThemeEditorView", "Installed, staged, disabled and missing framework banners."),
    QT_TRANSLATE_NOOP("ThemeEditorView", "Pinned Overwrite and Root Folder rows."),
    QT_TRANSLATE_NOOP("ThemeEditorView", "Window, panels, list rows and input fields - the app's surfaces."),
    QT_TRANSLATE_NOOP("ThemeEditorView", "Label and list text throughout the app, plus success/warning/error text."),
    QT_TRANSLATE_NOOP("ThemeEditorView", "The highlight colour: links, dropdown arrows and accented controls."),
    QT_TRANSLATE_NOOP("ThemeEditorView", "Lines and frames around panels, lists and inputs."),
    QT_TRANSLATE_NOOP("ThemeEditorView", "Danger / cancel / remove buttons (delete, remove profile, ✕ close)."),
    QT_TRANSLATE_NOOP("ThemeEditorView", "Success / confirm buttons (Install, Done, Play)."),
    QT_TRANSLATE_NOOP("ThemeEditorView", "Warning buttons (Reinstall, download / update actions)."),
    QT_TRANSLATE_NOOP("ThemeEditorView", "Info / neutral action buttons (Select, Groups, Plugin Rules)."),
    QT_TRANSLATE_NOOP("ThemeEditorView", "Secondary / neutral buttons (View, minor actions)."),
    QT_TRANSLATE_NOOP("ThemeEditorView", "Accent buttons like Ko-Fi."),
    QT_TRANSLATE_NOOP("ThemeEditorView", "Coloured labels in file trees (folders, BSA archives, bundled/installed)."),
    QT_TRANSLATE_NOOP("ThemeEditorView", "Shared accent tones reused by flags, icons and small highlights."),
    QT_TRANSLATE_NOOP("ThemeEditorView", "The scrollbar track and thumb."),
    QT_TRANSLATE_NOOP("ThemeEditorView", "Popup/overlay backgrounds and coloured info rows (required/optional mods, cards)."),
    QT_TRANSLATE_NOOP("ThemeEditorView", "Small status pills and badges (queued, download progress, error/success)."),
    QT_TRANSLATE_NOOP("ThemeEditorView", "Plugin-cycle rows and file-conflict colours in the Data / Mod Files views."),
    QT_TRANSLATE_NOOP("ThemeEditorView", "Row tints when a conflicting mod is selected (winning / overridden / anchor)."),
    QT_TRANSLATE_NOOP("ThemeEditorView", "The framework-status banner above the Plugins list (installed / staged / disabled / missing)."),
    QT_TRANSLATE_NOOP("ThemeEditorView", "The pinned Overwrite and Root Folder bands at the top of the modlist."),
    QT_TRANSLATE_NOOP("ThemeEditorView", "The fill colour of a ticked checkbox (the tick stays auto-contrasted)."),
)


class ThemeEditorView(QWidget):
    _SWATCH_W = 130

    def __init__(self, window):
        super().__init__()
        self._window = window
        self._pal = active_palette()            # for styling THIS view
        self.setObjectName("ThemeEditorView")

        # Working state -----------------------------------------------------
        self._palettes = load_palettes()
        self._names = load_display_names()
        self._advanced = False
        # id of the theme currently being edited (None until Save As on a
        # built-in; a custom id once loaded/saved) - controls Save vs Save As.
        self._editing_id: str | None = None
        self._working: dict = {}                 # palette being edited
        self._swatches: dict[str, QPushButton] = {}
        self._swatch_labels: dict[str, QLabel] = {}
        self._dirty = False                      # unsaved edits since last save
        self._closing = False

        # UI ----------------------------------------------------------------
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        self.setStyleSheet(self._qss())

        outer.addWidget(self._build_top_bar())

        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.NoFrame)

        # Left: colour swatches. Right: a compact preview that remains visible
        # while this full-screen editor covers the rest of the application.
        self._preview = ThemePreviewPanel()
        self._preview.rolesSelected.connect(self._reveal_preview_roles)
        split = QSplitter(Qt.Horizontal)
        split.addWidget(self._scroll)
        split.addWidget(self._preview)
        split.setStretchFactor(0, 1)
        split.setStretchFactor(1, 1)
        split.setSizes([600, 500])
        split.setChildrenCollapsible(True)   # either side can be hidden
        outer.addWidget(split, 1)

        # Seed from the current active theme.
        start_id = uc.get_appearance_mode() or "dark"
        if start_id not in self._palettes:
            start_id = "dark" if "dark" in self._palettes else next(
                iter(self._palettes), "dark")
        self._load_theme(start_id)

    # ---- styling ----------------------------------------------------------
    def _qss(self) -> str:
        c = lambda k: _c(self._pal, k)
        return f"""
        #ThemeEditorView {{ background:{c('BG_DEEP')}; }}
        QLabel {{ color:{c('TEXT_MAIN')}; }}
        QGroupBox {{
            color:{c('TEXT_MAIN')}; border:1px solid {c('BORDER')};
            border-radius:6px; margin-top:14px; font-weight:600;
        }}
        QGroupBox::title {{
            subcontrol-origin:margin; left:10px; padding:0 4px;
        }}
        #ThemeTopBar {{ background:{c('BG_HEADER')};
            border-bottom:1px solid {c('BORDER')}; }}
        QComboBox, QLineEdit {{
            background:{c('BG_ENTRY')}; color:{c('TEXT_MAIN')};
            border:1px solid {c('BORDER')}; border-radius:4px; padding:3px 6px;
        }}
        """

    # ---- top bar ----------------------------------------------------------
    def _build_top_bar(self) -> QWidget:
        bar = QFrame()
        bar.setObjectName("ThemeTopBar")
        h = QHBoxLayout(bar)
        h.setContentsMargins(14, 10, 14, 10)
        h.setSpacing(10)

        title = QLabel(self.tr("Theme Editor"))
        f = title.font(); f.setPointSize(f.pointSize() + 3); f.setBold(True)
        title.setFont(f)
        h.addWidget(title)

        h.addSpacing(12)
        h.addWidget(QLabel(self.tr("Start from:")))
        self._start_combo = QComboBox()
        no_wheel(self._start_combo)
        for tid, disp in self._names.items():
            self._start_combo.addItem(disp, tid)
        self._start_combo.activated.connect(self._on_start_changed)
        h.addWidget(self._start_combo)

        self._advanced_cb = QCheckBox(self.tr("Fine tune app-specific colours"))
        self._advanced_cb.toggled.connect(self._on_advanced_toggled)
        h.addWidget(self._advanced_cb)

        h.addStretch(1)

        self._save_btn = QPushButton(self.tr("Save"))
        self._save_btn.setObjectName("PrimaryButton")
        self._save_btn.setCursor(Qt.PointingHandCursor)
        self._save_btn.clicked.connect(lambda: self._save(save_as=False))
        h.addWidget(self._save_btn)

        self._save_as_btn = QPushButton(self.tr("Save As…"))
        self._save_as_btn.setObjectName("FormButton")
        self._save_as_btn.setCursor(Qt.PointingHandCursor)
        self._save_as_btn.clicked.connect(lambda: self._save(save_as=True))
        h.addWidget(self._save_as_btn)

        self._delete_btn = QPushButton(self.tr("Delete"))
        self._delete_btn.setObjectName("FormButton")
        self._delete_btn.setCursor(Qt.PointingHandCursor)
        self._delete_btn.clicked.connect(self._delete)
        h.addWidget(self._delete_btn)

        close = QPushButton(self.tr("✕ Close"))
        close.setObjectName("FormButton")
        close.setCursor(Qt.PointingHandCursor)
        close.clicked.connect(self._close_tab)
        h.addWidget(close)

        return bar

    # ---- body -------------------------------------------------------------
    def _load_theme(self, theme_id: str):
        """Seed the working palette from *theme_id* and rebuild the swatch grid."""
        # Start from the canonical full key set (dark) so a theme saved before a
        # new palette key existed still exposes that key for editing - otherwise
        # grouped_for_palette() filters out any group whose keys are absent from
        # the loaded palette (e.g. the Framework detection section on an older
        # custom theme). The selected theme's own values then override the seed.
        full = dict(self._palettes.get("dark", {}))
        base = self._palettes.get(theme_id, {})
        full.update(base)
        self._working = full
        self._editing_id = theme_id if ct.is_custom_theme(theme_id) else None
        # keep the combo in sync
        idx = self._start_combo.findData(theme_id)
        if idx >= 0:
            self._start_combo.setCurrentIndex(idx)
        self._delete_btn.setVisible(ct.is_custom_theme(theme_id))
        self._save_btn.setText(self.tr("Save") if self._editing_id
                               else self.tr("Save As New…"))
        # A built-in cannot be overwritten, so its primary action is already
        # Save As. Avoid presenting two buttons that perform the same action.
        self._save_as_btn.setVisible(self._editing_id is not None)
        self._dirty = False
        self._rebuild_body()
        # Seed the preview's explicit QPalette before the app-wide apply. The
        # runtime's selective repolish then resolves palette(...) expressions
        # in this subtree once, using the new working colours.
        self._preview.refresh(self._working, restyle=False)
        self._apply_working_preview()

    def _rebuild_body(self):
        body = QWidget()
        v = QVBoxLayout(body)
        v.setContentsMargins(16, 12, 16, 24)
        v.setSpacing(14)

        note = QLabel(self.tr(
            "Related colours are linked automatically. Enable fine tuning to "
            "adjust individual app-specific colours. Changes preview across "
            "the open app; save the theme to keep them."))
        note.setWordWrap(True)
        note.setStyleSheet(f"color:{_c(self._pal, 'TEXT_DIM')};")
        v.addWidget(note)

        self._swatches.clear()
        self._swatch_labels.clear()
        groups = (teg.grouped_for_palette(self._working) if self._advanced
                  else teg.simple_grouped_for_palette(self._working))
        descriptions = (teg.GROUP_DESCRIPTIONS if self._advanced
                        else teg.SIMPLE_GROUP_DESCRIPTIONS)
        for title, visible_keys in groups:
            box = QGroupBox(self.tr(title))
            grid = QGridLayout(box)
            grid.setContentsMargins(12, 8, 12, 12)
            grid.setHorizontalSpacing(12)
            grid.setVerticalSpacing(6)
            row = 0
            desc = descriptions.get(title)
            if desc:
                d = QLabel(self.tr(desc))
                d.setWordWrap(True)
                d.setStyleSheet(f"color:{_c(self._pal, 'TEXT_DIM')}; font-size:11px;")
                grid.addWidget(d, row, 0, 1, 3)
                row += 1
            for key, label in visible_keys:
                key_label = QLabel(self.tr(label))
                self._swatch_labels[key] = key_label
                grid.addWidget(key_label, row, 0)
                grid.addWidget(self._make_swatch(key), row, 1, Qt.AlignLeft)
                row += 1
            grid.setColumnStretch(2, 1)
            v.addWidget(box)

        v.addStretch(1)
        self._scroll.setWidget(body)

    def _make_swatch(self, key: str) -> QPushButton:
        btn = QPushButton()
        btn.setFixedWidth(self._SWATCH_W)
        btn.setCursor(Qt.PointingHandCursor)
        self._swatches[key] = btn
        self._paint_swatch(key)
        btn.clicked.connect(lambda _=False, k=key: self._pick_color(k))
        return btn

    def _paint_swatch(self, key: str):
        btn = self._swatches.get(key)
        if btn is None:
            return
        hexval = str(self._working.get(key, "#000000"))
        # readable text colour on the swatch
        rgb = teg._rgb(hexval) or (0, 0, 0)
        lum = 0.299 * rgb[0] + 0.587 * rgb[1] + 0.114 * rgb[2]
        fg = "#000000" if lum > 140 else "#ffffff"
        btn.setText(hexval)
        btn.setStyleSheet(
            f"QPushButton{{background:{hexval}; color:{fg};"
            f" border:1px solid {_c(self._pal,'BORDER')}; border-radius:4px;"
            f" padding:4px; font-family:monospace;}}")

    # ---- editing ----------------------------------------------------------
    def _pick_color(self, key: str):
        initial = QColor(str(self._working.get(key, "#000000")))
        ColorPickerOverlay.show_over(
            self, self.tr("Pick colour: {0}").format(key), initial,
            lambda col, k=key: self._on_color_picked(k, col))

    def _on_color_picked(self, key: str, color):
        if color is None or not color.isValid():
            return
        hexval = color.name()  # #rrggbb
        if self._advanced:
            self._working[key] = hexval
            self._paint_swatch(key)
        else:
            for k, v in teg.derive_simple(
                    key, hexval, self._working).items():
                self._working[k] = v
                self._paint_swatch(k)
        self._dirty = True
        self._preview.refresh(self._working, restyle=False)
        self._apply_working_preview()

    def _on_advanced_toggled(self, on: bool):
        self._advanced = bool(on)
        self._rebuild_body()

    def _reveal_preview_roles(self, _label: str, roles) -> None:
        """Reveal and highlight the settings used by a preview item."""
        role_keys = tuple(role for role in roles if role in teg.ROLE_LABELS)
        visible = [role for role in role_keys if role in self._swatches]
        if not visible and role_keys and not self._advanced:
            # App-specific samples such as framework or plugin-cycle rows have
            # no compact equivalent. Fine tuning is the only place their exact
            # roles can be shown, so reveal it automatically.
            self._advanced_cb.setChecked(True)
            visible = [role for role in role_keys if role in self._swatches]
        if not visible:
            return

        self._clear_role_highlight()
        accent = _c(self._working, "ACCENT")
        for role in visible:
            label = self._swatch_labels.get(role)
            if label is not None:
                label.setStyleSheet(
                    f"color:{accent}; font-weight:700;")

        target = self._swatches[visible[0]]
        QTimer.singleShot(
            0, lambda w=target: self._scroll.ensureWidgetVisible(w, 40, 100))

    def _clear_role_highlight(self) -> None:
        for label in self._swatch_labels.values():
            label.setStyleSheet("")

    def _on_start_changed(self, idx: int):
        tid = self._start_combo.itemData(idx)
        if tid:
            self._load_theme(tid)

    # ---- save / delete ----------------------------------------------------
    def _save(self, save_as: bool):
        if not save_as and self._editing_id:
            self._do_save(self._editing_id,
                          self._names.get(self._editing_id, "Theme"))
            return
        # Save As (or a first save on a built-in): ask for a name.
        from gui_qt.text_input_overlay import TextInputOverlay
        suggestion = self._names.get(self._start_combo.currentData(), "My Theme")
        TextInputOverlay.show_over(
            self, self.tr("Save Theme"), self.tr("Theme name:"),
            self._on_name_entered,
            initial=suggestion, ok_label=self.tr("Save"))

    def _on_name_entered(self, name):
        if not name or not name.strip():
            return
        self._do_save(None, name.strip())

    def _do_save(self, theme_id, name, overwrite: bool = False) -> str | None:
        # Base clone from the palette we started from guarantees a full key set.
        start_id = self._start_combo.currentData() or "dark"
        base = self._palettes.get(start_id, {})
        appearance = get_ctk_appearance(start_id)
        try:
            new_id = ct.save_custom_theme(
                name, self._working, ctk_appearance=appearance,
                base_palette=base, theme_id=theme_id, overwrite=overwrite)
        except Exception as exc:
            ConfirmOverlay.show_message(
                self, self.tr("Save failed"), str(exc))
            return None
        # Refresh discovery so the new theme is selectable everywhere.
        self._palettes = load_palettes()
        self._names = load_display_names()
        self._editing_id = new_id
        self._dirty = False
        # Make the saved theme active now and on subsequent launches.
        try:
            uc.save_appearance_mode(new_id)
        except Exception:
            pass
        app = QApplication.instance()
        if app is not None:
            apply_theme(app)
        self._refresh_start_combo(new_id)
        self._refresh_open_theme_selectors(new_id)
        self._delete_btn.setVisible(True)
        self._save_btn.setText(self.tr("Save"))
        self._save_as_btn.setVisible(True)
        return new_id

    def _apply_working_preview(self):
        app = QApplication.instance()
        if app is not None and self._working:
            apply_theme(app, self._working)

    def _refresh_open_theme_selectors(self, select_id: str | None = None):
        app = QApplication.instance()
        if app is None:
            return
        for widget in app.allWidgets():
            refresh = getattr(widget, "refresh_theme_options", None)
            if callable(refresh):
                try:
                    refresh(select_id)
                except (RuntimeError, TypeError):
                    pass

    def _refresh_start_combo(self, select_id):
        self._start_combo.blockSignals(True)
        self._start_combo.clear()
        for tid, disp in self._names.items():
            self._start_combo.addItem(disp, tid)
        idx = self._start_combo.findData(select_id)
        if idx >= 0:
            self._start_combo.setCurrentIndex(idx)
        self._start_combo.blockSignals(False)

    def _delete(self):
        tid = self._editing_id
        if not tid or not ct.is_custom_theme(tid):
            return
        name = self._names.get(tid, tid)
        ConfirmOverlay.show_over(
            self, self.tr("Delete theme?"),
            self.tr("Delete the custom theme \"{0}\"? This cannot be undone.")
                .format(name),
            lambda ok: self._do_delete(tid) if ok else None,
            confirm_label=self.tr("Delete"), danger=True)

    def _do_delete(self, tid):
        ct.delete_custom_theme(tid)
        # If the deleted theme was active, fall back to dark.
        try:
            if uc.get_appearance_mode() == tid:
                uc.save_appearance_mode("dark")
        except Exception:
            pass
        self._palettes = load_palettes()
        self._names = load_display_names()
        self._refresh_open_theme_selectors()
        self._refresh_start_combo("dark")
        self._load_theme("dark" if "dark" in self._palettes
                         else next(iter(self._palettes), "dark"))

    # ---- lifecycle --------------------------------------------------------
    def _close_tab(self):
        tabs = getattr(self._window, "_tabs", None)
        if tabs is not None:
            try:
                tabs.close_tab("theme_editor")
                return
            except Exception:
                pass
        self.tab_closing()
        self.hide()

    def tab_closing(self):
        """Restore the persisted theme before this editor is destroyed."""
        if self._closing:
            return
        self._closing = True
        app = QApplication.instance()
        if app is not None:
            apply_theme(app)
