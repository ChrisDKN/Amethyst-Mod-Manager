"""
Breeze Dark theme - matches the KDE Breeze Dark colour scheme (the look in the
Qt mockup). Same key set as dark.py / light.py; backgrounds use Breeze's
blue-grey family (#232629 / #2a2e32 / #31363b), the accent is Breeze blue
(#3daee9), and views sit on the darker Breeze "view" background.

Every key here must also exist in every other theme file. If you add a new
constant, add it to every theme or the app will break when that theme is
selected.
"""

NAME = "Breeze Dark"

CTK_APPEARANCE = "dark"

PALETTE: dict[str, str | tuple] = {
    # Backgrounds - Breeze "window" / "view" / "button" greys.
    "BG_DEEP":       "#232629",   # window background
    "BG_PANEL":      "#2a2e32",   # raised panel
    "BG_HEADER":     "#31363b",   # toolbar / header / button base
    "BG_ROW":        "#2d3135",
    "BG_ROW_ALT":    "#31363b",   # zebra alt
    "BG_ROW_HOVER":  "#3b4045",
    "BG_LIST":       "#1b1e20",   # Breeze "view" background (lists/trees)
    "BG_SEP":        "#31363b",
    "BG_HOVER":      "#2d5a7a",
    "BG_SELECT":     "#3daee9",   # Breeze selection blue
    "BG_HOVER_ROW":  "#3b4045",

    # Accents - Breeze blue.
    "ACCENT":        "#3daee9",
    "ACCENT_HOV":    "#5cbef0",
    "TEXT_ON_ACCENT":"#ffffff",

    # Text - Breeze foreground greys.
    "TEXT_MAIN":     "#fcfcfc",
    "TEXT_DIM":      "#a1a9b1",
    "TEXT_MUTED":    "#bdc3c7",
    "TEXT_FAINT":    "#7f8c8d",
    "TEXT_SEP":      "#c8ccd0",
    "TEXT_WHITE":    "#ffffff",
    "TEXT_BLACK":    "#000000",
    "TEXT_OK":       "#27ae60",
    "TEXT_ERR":      "#da4453",
    "TEXT_WARN":     "#f67400",
    "TEXT_OK_BRIGHT":   "#2ecc71",
    "TEXT_ERR_BRIGHT":  "#ed1515",
    "TEXT_WARN_BRIGHT": "#fdbc4b",

    # Borders - subtle Breeze separators.
    "BORDER":        "#3b4045",
    "BORDER_DIM":    "#454c54",
    "BORDER_FAINT":  "#565e66",

    # Buttons - reds
    "RED_BTN":       "#da4453",
    "RED_HOV":       "#ed1515",
    "BTN_DANGER":        "#da4453",
    "BTN_DANGER_HOV":    "#ed1515",
    "BTN_DANGER_ALT":    "#a02c2c",
    "BTN_DANGER_ALT_HOV":"#c0392b",
    "BTN_DANGER_DEEP":   "#7a1f1f",
    "BTN_DANGER_DEEP_HOV":"#a02525",
    "BTN_CANCEL":        "#c0392b",
    "BTN_CANCEL_HOV":    "#a93226",

    # Buttons - greens
    "BTN_SUCCESS":          "#27ae60",
    "BTN_SUCCESS_HOV":      "#2ecc71",
    "BTN_SUCCESS_ALT":      "#229954",
    "BTN_SUCCESS_ALT_HOV":  "#27ae60",
    "BTN_SUCCESS_DEEP":     "#1e8449",
    "BTN_SUCCESS_DEEP_HOV": "#239b56",

    # Buttons - oranges
    "BTN_WARN":          "#f67400",
    "BTN_WARN_HOV":      "#fb8c00",
    "BTN_WARN_DEEP":     "#a85100",
    "BTN_WARN_DEEP_HOV": "#cf6300",
    "BTN_WARN_BROWN":    "#6b3c00",
    "BTN_WARN_BROWN_HOV":"#8a4f00",
    "BTN_WARN_ORANGE":   "#c46200",
    "BTN_WARN_ORANGE_HOV":"#e87400",

    # Buttons - blues
    "BTN_INFO":          "#2980b9",
    "BTN_INFO_HOV":      "#3498db",
    "BTN_INFO_DEEP":     "#1f6593",
    "BTN_INFO_DEEP_HOV": "#2980b9",
    "BTN_NEUTRAL":       "#3a6ea5",
    "BTN_NEUTRAL_HOV":   "#4a82c0",

    # Buttons - greys
    "BTN_GREY":        "#31363b",
    "BTN_GREY_HOV":    "#3b4045",
    "BTN_GREY_ALT":    "#2d3135",
    "BTN_GREY_ALT_HOV":"#3b4045",

    # Buttons - purples
    "BTN_PURPLE":     "#9b59b6",
    "BTN_PURPLE_HOV": "#af6fc9",

    # Tree tags
    "TAG_FOLDER":       "#3daee9",
    "TAG_BSA":          "#f67400",
    "TAG_BSA_ALT":      "#56d8e4",
    "TAG_INI_PROFILE":  "#1abc9c",
    "TAG_BUNDLED_FG":   "#7ab8e8",
    "TAG_BUNDLED_BG":   "#1f2d3a",
    "TAG_INSTALLED_BG": "#1e4d2b",
    "TAG_UNORDERED_FG": "#7f8c8d",

    # Tones
    "TONE_GREEN":     "#27ae60",
    "TONE_RED":       "#da4453",
    "TONE_BLUE":      "#3daee9",
    "TONE_CYAN":      "#1abc9c",
    "TONE_BLUE_SOFT": "#61afef",
    "TONE_FLAG":      "#fdbc4b",

    # Scrollbars
    "SCROLL_BG":     "#31363b",
    "SCROLL_TROUGH": "#1b1e20",
    "SCROLL_ACTIVE": "#3daee9",

    # Overlays / special
    "BG_OVERLAY_ERR":  "#2a1f24",
    "BG_OVERLAY_DEEP": "#1b1e20",
    "BG_CARD":         "#31363b",
    "BG_CARD_ALT":     "#2a2e32",
    "BG_GREEN_ROW":    "#1e4d2b",
    "BG_GREEN_DEEP":   "#15401f",
    "BG_RED_DEEP":     "#4a1c22",
    "BG_ORANGE_DEEP":  "#5a3410",
    "BG_GREEN_TEXT":   "#c8ffc8",
    "BG_RED_TEXT":     "#ffc8c8",
    "BG_ORANGE_TEXT":  "#ffe0b0",
    "BG_BLUE_DEEP":    "#1a3a5c",
    "BG_BLUE_TEXT":    "#b0d8ff",
    "BG_DARK_BLUE":    "#1d2733",
    "BG_DARK_GREEN":   "#1c2a1e",
    "BG_ENTRY":        "#1b1e20",
    "BG_BTN_SAVE":     "#3a5a8a",
    "BG_SELECT_BAR":   "#2d4263",
    "BG_MOD_REQ":      "#27ae60",
    "BG_MOD_OPT":      "#f67400",

    # Status
    "STATUS_ERR_BRIGHT":    "#ff6b6b",
    "STATUS_BADGE_RED":     "#da4453",
    "STATUS_BADGE_GREEN":   "#27ae60",
    "STATUS_SUCCESS_SOLID": "#2ecc71",
    "STATUS_QUEUED":        "#f67400",
    "STATUS_DL_GREEN":      "#27ae60",

    # Card text
    "TEXT_CARD":     "#d8dce0",
    "TEXT_CARD_DIM": "#7f8c8d",
    "TEXT_CARD_MED": "#e8eaed",
    "TEXT_TREE_FG":  "#2ecc71",

    # CTk light/dark tuples
    "CTK_TEXT":       ("#000000", "#FFFFFF"),
    "CTK_FOOTER_FG":  ("#EBECF0", "#31363B"),
    "CTK_FOOTER_HOV": ("#DFE1E5", "#3B4045"),
    "CTK_SEP":        ("#C9CCD6", "#3B4045"),
    "CTK_SEP_ALT":    ("#D0D0D0", "#454C54"),
    "CTK_BTN_HOVER":  ("gray90", "gray25"),

    # Dropdown / combobox arrow glyph (tinted via QSS-generated PNG)
    "DROPDOWN_ARROW": "#25abe8",

    # Misc
    "LINK_BLUE":     "#3daee9",

    # Plugin-cycle status rows (Show Cycle view)
    "PLUGIN_CYCLE_ERR_BG":  "#6b3333",
    "PLUGIN_CYCLE_ERR_FG":  "#ffd9d9",
    "PLUGIN_CYCLE_OK_BG":   "#2f5d3a",
    "PLUGIN_CYCLE_OK_FG":   "#dcf5dc",
    "PLUGIN_CYCLE_WARN_BG": "#4a4320",
    "PLUGIN_CYCLE_WARN_FG": "#f5e28a",
    "PLUGIN_CYCLE_ANCHOR":  "#e89862",
    "PLUGIN_CYCLE_LINK":    "#62b0e8",

    # File conflict states (Data / Mod Files / plugin conflicts)
    "FILE_WIN":      "#108d00",
    "FILE_LOSE":     "#9a0e0e",
    "FILE_DIM":      "#7a7a7a",
    "FILE_ANCHOR":   "#A45500",

    # Drag selection outline (modlist / plugins)
    "HIGHLIGHT_DRAG": "#5aa9ff",

    # Cross-panel conflict row highlights (modlist / plugins / data tree)
    "CONFLICT_HL_WIN":    "#108d00",   # selection beats this mod (green)
    "CONFLICT_HL_LOSE":   "#9a0e0e",   # this mod beats selection (red)
    "CONFLICT_HL_ANCHOR": "#A45500",   # plugin-selected / anchor mod (orange)
    "REQ_HL_REQUIRES":    "#5e3a8e",   # mods the selection requires (purple)
    "REQ_HL_REQUIRED_BY": "#1d4f8b",   # mods that require the selection (blue)

    # Framework-status banner rows (Plugins tab) - per install state
    "FRAMEWORK_INSTALLED_BG": "#15401f", "FRAMEWORK_INSTALLED_FG": "#c8ffc8",
    "FRAMEWORK_STAGED_BG":    "#5a3410", "FRAMEWORK_STAGED_FG":    "#ffe0b0",
    "FRAMEWORK_DISABLED_BG":  "#1a3a5c", "FRAMEWORK_DISABLED_FG":  "#b0d8ff",
    "FRAMEWORK_MISSING_BG":   "#4a1c22", "FRAMEWORK_MISSING_FG":   "#ffc8c8",

    # Modlist boundary separator bands (pinned Overwrite / Root Folder rows)
    "OVERWRITE_SEP_BG": "#1c2a1e", "OVERWRITE_SEP_FG": "#2ecc71",
    "ROOT_SEP_BG":      "#1d2733", "ROOT_SEP_FG":      "#61afef",

    # Checkbox fill when checked (tick auto-contrasts off this)
    "CHECK_FILL": "#3daee9",
}
