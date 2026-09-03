"""
Cyberpunk theme - inspired by the Cyberpunk 2077 / Mod Organizer look.

Deep red-black backgrounds, red separators, cyan text and yellow buttons /
selection. Every key mirrors dark.py - if a key is added there, add it here too
or the app breaks when this theme is selected.
"""

NAME = "Cyberpunk"

# Dark-based custom theme; built-in CTk widgets use the dark appearance.
CTK_APPEARANCE = "dark"

# Signature Cyberpunk colours
#   yellow   #f3e600 / #eeee00   (accent / buttons / selection)
#   red      #ff003c             (danger / scrollbar / borders)
#   cyan     #00ffff             (primary text / info & warn buttons)

PALETTE: dict[str, str | tuple] = {
    # Backgrounds - deep red-black
    "BG_DEEP":       "#120608",
    "BG_PANEL":      "#1a080b",
    "BG_HEADER":     "#200a0e",
    "BG_ROW":        "#160709",
    "BG_ROW_ALT":    "#1e0a0e",
    "BG_ROW_HOVER":  "#331016",
    "BG_LIST":       "#120607",
    "BG_SEP":        "#7f0a1d",   # solid red separator bar (matches MO2 screenshot)
    "BG_HOVER":      "#4a0012",
    "BG_SELECT":     "#eeee00",   # dark red glow selection (matches screenshot)
    "BG_HOVER_ROW":  "#331016",

    # Accents - electric yellow
    "ACCENT":        "#f3e600",
    "ACCENT_HOV":    "#fff340",
    "TEXT_ON_ACCENT":"#0a0e12",   # dark text reads on bright yellow

    # Text - cyan family
    "TEXT_MAIN":     "#00ffff",
    "TEXT_DIM":      "#008b8b",
    "TEXT_MUTED":    "#66c2d4",
    "TEXT_FAINT":    "#3d7580",
    "TEXT_SEP":      "#00ffff",   # light text on the red separator bar
    "TEXT_WHITE":    "#00ffff",   # "white" text reads as cyan in this theme
    "TEXT_BLACK":    "#000000",
    "TEXT_OK":       "#5ce6b0",
    "TEXT_ERR":      "#ff4d6a",
    "TEXT_WARN":     "#f3e600",
    "TEXT_OK_BRIGHT":   "#3ef0a0",
    "TEXT_ERR_BRIGHT":  "#ff3355",
    "TEXT_WARN_BRIGHT": "#ffdb2e",

    # Borders - red-tinted to match the screenshot's red grid/frame
    "BORDER":        "#5a1420",
    "BORDER_DIM":    "#6e1a28",
    "BORDER_FAINT":  "#822032",

    # Buttons - reds
    "RED_BTN":       "#c00028",
    "RED_HOV":       "#ff003c",
    "BTN_DANGER":        "#c00028",
    "BTN_DANGER_HOV":    "#ff003c",
    "BTN_DANGER_ALT":    "#8b0020",
    "BTN_DANGER_ALT_HOV":"#c00028",
    "BTN_DANGER_DEEP":   "#6a0018",
    "BTN_DANGER_DEEP_HOV":"#970022",
    "BTN_CANCEL":        "#c00028",
    "BTN_CANCEL_HOV":    "#970022",

    # Buttons - greens (teal-leaning to fit the palette)
    "BTN_SUCCESS":          "#eeee00",
    "BTN_SUCCESS_HOV":      "#f0f028",
    "BTN_SUCCESS_ALT":      "#969600",
    "BTN_SUCCESS_ALT_HOV":  "#a4a423",
    "BTN_SUCCESS_DEEP":     "#5f5f00",
    "BTN_SUCCESS_DEEP_HOV": "#757523",

    # Buttons - oranges/yellows
    "BTN_WARN":          "#00ffff",
    "BTN_WARN_HOV":      "#23ffff",
    "BTN_WARN_DEEP":     "#009494",
    "BTN_WARN_DEEP_HOV": "#23a2a2",
    "BTN_WARN_BROWN":    "#009c9c",
    "BTN_WARN_BROWN_HOV":"#1ea7a7",
    "BTN_WARN_ORANGE":   "#00ffff",
    "BTN_WARN_ORANGE_HOV":"#23ffff",

    # Buttons - blues/cyans
    "BTN_INFO":          "#00ffff",
    "BTN_INFO_HOV":      "#38ffff",
    "BTN_INFO_DEEP":     "#009c9c",
    "BTN_INFO_DEEP_HOV": "#23a9a9",
    "BTN_NEUTRAL":       "#004c4c",
    "BTN_NEUTRAL_HOV":   "#236565",

    # Buttons - greys
    "BTN_GREY":        "#1e2c36",
    "BTN_GREY_HOV":    "#2a3d4a",
    "BTN_GREY_ALT":    "#182430",
    "BTN_GREY_ALT_HOV":"#243541",

    # Buttons - purples/magenta
    "BTN_PURPLE":     "#eeee00",
    "BTN_PURPLE_HOV": "#f0f028",

    # Tree tags
    "TAG_FOLDER":       "#00e5ff",
    "TAG_BSA":          "#f3e600",
    "TAG_BSA_ALT":      "#00e5ff",
    "TAG_INI_PROFILE":  "#00e5ff",
    "TAG_BUNDLED_FG":   "#66d9ff",
    "TAG_BUNDLED_BG":   "#0a2833",
    "TAG_INSTALLED_BG": "#eeee00",
    "TAG_UNORDERED_FG": "#6d7d88",

    # Tones
    "TONE_GREEN":     "#5ce6b0",
    "TONE_RED":       "#ff4d6a",
    "TONE_BLUE":      "#00e5ff",
    "TONE_CYAN":      "#00e5ff",
    "TONE_BLUE_SOFT": "#66d9ff",
    "TONE_FLAG":      "#f3e600",

    # Scrollbars - red thumb (matches screenshot)
    "SCROLL_BG":     "#3a1018",
    "SCROLL_TROUGH": "#120607",
    "SCROLL_ACTIVE": "#f3e600",

    # Overlays / special
    "BG_OVERLAY_ERR":  "#180810",
    "BG_OVERLAY_DEEP": "#0a0e12",
    "BG_CARD":         "#141b22",
    "BG_CARD_ALT":     "#0f1419",
    "BG_GREEN_ROW":    "#0a4a34",
    "BG_GREEN_DEEP":   "#0a3a28",
    "BG_RED_DEEP":     "#4a0a18",
    "BG_ORANGE_DEEP":  "#4a3a00",
    "BG_GREEN_TEXT":   "#a8ffe0",
    "BG_RED_TEXT":     "#ffc8d2",
    "BG_ORANGE_TEXT":  "#fff0a0",
    "BG_BLUE_DEEP":    "#0a2a4a",
    "BG_BLUE_TEXT":    "#a0e8ff",
    "BG_DARK_BLUE":    "#0a1220",
    "BG_DARK_GREEN":   "#0a1a14",
    "BG_ENTRY":        "#0a0f14",
    "BG_BTN_SAVE":     "#1a4a6a",
    "BG_SELECT_BAR":   "#2a1020",
    "BG_MOD_REQ":      "#008a5e",
    "BG_MOD_OPT":      "#c9a400",

    # Status
    "STATUS_ERR_BRIGHT":    "#ff3355",
    "STATUS_BADGE_RED":     "#ff003c",
    "STATUS_BADGE_GREEN":   "#00b878",
    "STATUS_SUCCESS_SOLID": "#00ffaa",
    "STATUS_QUEUED":        "#f3e600",
    "STATUS_DL_GREEN":      "#00e090",

    # Card text - cyan family
    "TEXT_CARD":     "#00ffff",
    "TEXT_CARD_DIM": "#3d7580",
    "TEXT_CARD_MED": "#a8ecf6",
    "TEXT_TREE_FG":  "#3ef0a0",

    # CTk light/dark tuples - keep tuples so built-in CTk widgets still adapt.
    "CTK_TEXT":       ("#000000", "#FFFFFF"),
    "CTK_FOOTER_FG":  ("#EBECF0", "#12171D"),
    "CTK_FOOTER_HOV": ("#DFE1E5", "#1C2733"),
    "CTK_SEP":        ("#C9CCD6", "#243541"),
    "CTK_SEP_ALT":    ("#D0D0D0", "#2E4451"),
    "CTK_BTN_HOVER":  ("gray90", "gray20"),

    # Dropdown / combobox arrow glyph (tinted via QSS-generated PNG)
    "DROPDOWN_ARROW": "#f3e600",

    # Misc
    "LINK_BLUE":     "#00e5ff",

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
    "CONFLICT_HL_WIN":    "#119300",   # selection beats this mod (green)
    "CONFLICT_HL_LOSE":   "#ff1717",   # this mod beats selection (red)
    "CONFLICT_HL_ANCHOR": "#A45500",   # plugin-selected / anchor mod (orange)
    "REQ_HL_REQUIRES":    "#8a2be2",   # mods the selection requires (purple)
    "REQ_HL_REQUIRED_BY": "#1e6fd9",   # mods that require the selection (blue)

    # Framework-status banner rows (Plugins tab) - per install state
    "FRAMEWORK_INSTALLED_BG": "#b1b100", "FRAMEWORK_INSTALLED_FG": "#000000",
    "FRAMEWORK_STAGED_BG":    "#8c0096", "FRAMEWORK_STAGED_FG":    "#ffffff",
    "FRAMEWORK_DISABLED_BG":  "#00727a", "FRAMEWORK_DISABLED_FG":  "#000000",
    "FRAMEWORK_MISSING_BG":   "#9f0003", "FRAMEWORK_MISSING_FG":   "#ffffff",

    # Modlist boundary separator bands (pinned Overwrite / Root Folder rows)
    "OVERWRITE_SEP_BG": "#5a1420", "OVERWRITE_SEP_FG": "#00ffff",
    "ROOT_SEP_BG":      "#5a1420", "ROOT_SEP_FG":      "#00ffff",

    # Checkbox fill when checked (tick auto-contrasts off this)
    "CHECK_FILL": "#f3e600",

}
