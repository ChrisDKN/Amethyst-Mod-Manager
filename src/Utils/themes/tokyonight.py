"""Tokyo Night - a cool, high-contrast midnight theme.

The palette follows the Tokyo Night colour family: ink-blue surfaces, soft
lavender text, a periwinkle-blue accent, and vivid semantic colours.  Every
key mirrors dark.py so the theme covers the entire application.
"""

NAME = "Tokyo Night"

CTK_APPEARANCE = "dark"

# Theme-aware defaults for legacy/Tk conflict and separator colour hooks.
THEME_DEFAULTS_OVERRIDE: dict[str, str] = {
    "conflict_higher":    "#315a45",
    "conflict_lower":     "#713b4b",
    "plugin_mod":         "#79543e",
    "plugin_separator":   "#79543e",
    "conflict_separator": "#3b4261",
    "separator_bg":       "#292e42",
}

PALETTE: dict[str, str | tuple] = {
    # Backgrounds - Tokyo Night's layered ink-blue surfaces.
    "BG_DEEP":       "#1a1b26",
    "BG_PANEL":      "#202334",
    "BG_HEADER":     "#24283b",
    "BG_ROW":        "#24283b",
    "BG_ROW_ALT":    "#292e42",
    "BG_ROW_HOVER":  "#343b58",
    "BG_LIST":       "#16161e",
    "BG_SEP":        "#292e42",
    "BG_HOVER":      "#283457",
    "BG_SELECT":     "#7aa2f7",
    "BG_HOVER_ROW":  "#343b58",

    # Accents - the signature Tokyo Night blue.
    "ACCENT":        "#7aa2f7",
    "ACCENT_HOV":    "#89b4fa",
    "TEXT_ON_ACCENT":"#1a1b26",

    # Text - cool lavender-white with the canonical comment-blue ramp.
    "TEXT_MAIN":     "#c0caf5",
    "TEXT_DIM":      "#737aa2",
    "TEXT_MUTED":    "#a9b1d6",
    "TEXT_FAINT":    "#565f89",
    "TEXT_SEP":      "#a9b1d6",
    "TEXT_WHITE":    "#ffffff",
    "TEXT_BLACK":    "#1a1b26",
    "TEXT_OK":       "#9ece6a",
    "TEXT_ERR":      "#f7768e",
    "TEXT_WARN":     "#e0af68",
    "TEXT_OK_BRIGHT":   "#b9e27c",
    "TEXT_ERR_BRIGHT":  "#ff8fa3",
    "TEXT_WARN_BRIGHT": "#f2c879",

    # Borders - quiet blue-grey divisions.
    "BORDER":        "#3b4261",
    "BORDER_DIM":    "#414868",
    "BORDER_FAINT":  "#545c7e",

    # Buttons - reds.
    "RED_BTN":       "#c53b53",
    "RED_HOV":       "#db4b4b",
    "BTN_DANGER":        "#c53b53",
    "BTN_DANGER_HOV":    "#db4b4b",
    "BTN_DANGER_ALT":    "#a33a4f",
    "BTN_DANGER_ALT_HOV":"#bd465a",
    "BTN_DANGER_DEEP":   "#713b4b",
    "BTN_DANGER_DEEP_HOV":"#914052",
    "BTN_CANCEL":        "#b84a5f",
    "BTN_CANCEL_HOV":    "#c95a6d",

    # Buttons - greens.
    "BTN_SUCCESS":          "#4f805d",
    "BTN_SUCCESS_HOV":      "#65a36f",
    "BTN_SUCCESS_ALT":      "#3d684c",
    "BTN_SUCCESS_ALT_HOV":  "#4f805d",
    "BTN_SUCCESS_DEEP":     "#31523d",
    "BTN_SUCCESS_DEEP_HOV": "#3d684c",

    # Buttons - oranges.
    "BTN_WARN":          "#b87b4b",
    "BTN_WARN_HOV":      "#d18c56",
    "BTN_WARN_DEEP":     "#845936",
    "BTN_WARN_DEEP_HOV": "#9c6840",
    "BTN_WARN_BROWN":    "#68472f",
    "BTN_WARN_BROWN_HOV":"#805638",
    "BTN_WARN_ORANGE":   "#b86f44",
    "BTN_WARN_ORANGE_HOV":"#d28350",

    # Buttons - blues.
    "BTN_INFO":          "#3d59a1",
    "BTN_INFO_HOV":      "#5478c8",
    "BTN_INFO_DEEP":     "#30487f",
    "BTN_INFO_DEEP_HOV": "#3d59a1",
    "BTN_NEUTRAL":       "#4d5880",
    "BTN_NEUTRAL_HOV":   "#626e9a",

    # Buttons - blue-grey neutrals.
    "BTN_GREY":        "#343b58",
    "BTN_GREY_HOV":    "#414868",
    "BTN_GREY_ALT":    "#292e42",
    "BTN_GREY_ALT_HOV":"#3b4261",

    # Buttons - purples.
    "BTN_PURPLE":     "#7c5eb0",
    "BTN_PURPLE_HOV": "#9d7cd8",

    # Tree tags.
    "TAG_FOLDER":       "#7dcfff",
    "TAG_BSA":          "#e0af68",
    "TAG_BSA_ALT":      "#2ac3de",
    "TAG_INI_PROFILE":  "#73daca",
    "TAG_BUNDLED_FG":   "#7aa2f7",
    "TAG_BUNDLED_BG":   "#202f55",
    "TAG_INSTALLED_BG": "#263b36",
    "TAG_UNORDERED_FG": "#565f89",

    # Tones.
    "TONE_GREEN":     "#9ece6a",
    "TONE_RED":       "#f7768e",
    "TONE_BLUE":      "#7aa2f7",
    "TONE_CYAN":      "#7dcfff",
    "TONE_BLUE_SOFT": "#89b4fa",
    "TONE_FLAG":      "#e0af68",

    # Scrollbars.
    "SCROLL_BG":     "#292e42",
    "SCROLL_TROUGH": "#16161e",
    "SCROLL_ACTIVE": "#7aa2f7",

    # Overlays / special surfaces.
    "BG_OVERLAY_ERR":  "#30202d",
    "BG_OVERLAY_DEEP": "#16161e",
    "BG_CARD":         "#24283b",
    "BG_CARD_ALT":     "#202334",
    "BG_GREEN_ROW":    "#263b36",
    "BG_GREEN_DEEP":   "#20332e",
    "BG_RED_DEEP":     "#3b2432",
    "BG_ORANGE_DEEP":  "#423326",
    "BG_GREEN_TEXT":   "#b9e27c",
    "BG_RED_TEXT":     "#ff9aae",
    "BG_ORANGE_TEXT":  "#f2c879",
    "BG_BLUE_DEEP":    "#202f55",
    "BG_BLUE_TEXT":    "#a9c6ff",
    "BG_DARK_BLUE":    "#1b2238",
    "BG_DARK_GREEN":   "#1d2b29",
    "BG_ENTRY":        "#16161e",
    "BG_BTN_SAVE":     "#3d59a1",
    "BG_SELECT_BAR":   "#283457",
    "BG_MOD_REQ":      "#4f805d",
    "BG_MOD_OPT":      "#b87b4b",

    # Status colours.
    "STATUS_ERR_BRIGHT":    "#ff8fa3",
    "STATUS_BADGE_RED":     "#db4b4b",
    "STATUS_BADGE_GREEN":   "#73a959",
    "STATUS_SUCCESS_SOLID": "#9ece6a",
    "STATUS_QUEUED":        "#ff9e64",
    "STATUS_DL_GREEN":      "#73daca",

    # Card text.
    "TEXT_CARD":     "#c0caf5",
    "TEXT_CARD_DIM": "#737aa2",
    "TEXT_CARD_MED": "#a9b1d6",
    "TEXT_TREE_FG":  "#9ece6a",

    # CTk light/dark tuples.
    "CTK_TEXT":       ("#1a1b26", "#c0caf5"),
    "CTK_FOOTER_FG":  ("#e8eaf2", "#24283b"),
    "CTK_FOOTER_HOV": ("#dce0eb", "#343b58"),
    "CTK_SEP":        ("#c8ccda", "#3b4261"),
    "CTK_SEP_ALT":    ("#d3d6e0", "#414868"),
    "CTK_BTN_HOVER":  ("gray90", "#343b58"),

    # Dropdown / combobox arrow glyph.
    "DROPDOWN_ARROW": "#7aa2f7",

    # Links.
    "LINK_BLUE":     "#7dcfff",

    # Plugin-cycle status rows.
    "PLUGIN_CYCLE_ERR_BG":  "#3b2432",
    "PLUGIN_CYCLE_ERR_FG":  "#ff9aae",
    "PLUGIN_CYCLE_OK_BG":   "#263b36",
    "PLUGIN_CYCLE_OK_FG":   "#b9e27c",
    "PLUGIN_CYCLE_WARN_BG": "#423326",
    "PLUGIN_CYCLE_WARN_FG": "#f2c879",
    "PLUGIN_CYCLE_ANCHOR":  "#ff9e64",
    "PLUGIN_CYCLE_LINK":    "#7dcfff",

    # File conflict states.
    "FILE_WIN":      "#4f805d",
    "FILE_LOSE":     "#a33a4f",
    "FILE_DIM":      "#565f89",
    "FILE_ANCHOR":   "#9c6840",

    # Drag selection outline.
    "HIGHLIGHT_DRAG": "#89b4fa",

    # Cross-panel conflict row highlights.
    "CONFLICT_HL_WIN":    "#315a45",
    "CONFLICT_HL_LOSE":   "#713b4b",
    "CONFLICT_HL_ANCHOR": "#79543e",
    "REQ_HL_REQUIRES":    "#584475",
    "REQ_HL_REQUIRED_BY": "#314d79",

    # Framework-status banner rows.
    "FRAMEWORK_INSTALLED_BG": "#263b36", "FRAMEWORK_INSTALLED_FG": "#b9e27c",
    "FRAMEWORK_STAGED_BG":    "#423326", "FRAMEWORK_STAGED_FG":    "#f2c879",
    "FRAMEWORK_DISABLED_BG":  "#202f55", "FRAMEWORK_DISABLED_FG":  "#a9c6ff",
    "FRAMEWORK_MISSING_BG":   "#3b2432", "FRAMEWORK_MISSING_FG":   "#ff9aae",

    # Modlist boundary separator bands.
    "OVERWRITE_SEP_BG": "#1d2b29", "OVERWRITE_SEP_FG": "#9ece6a",
    "ROOT_SEP_BG":      "#1b2238", "ROOT_SEP_FG":      "#7aa2f7",

    # Checkbox fill when checked.
    "CHECK_FILL": "#7aa2f7",
}
