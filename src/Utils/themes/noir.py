"""Noir - a high-contrast monochrome theme.

Inky black surfaces, silver controls, and warm-white type give the interface a
restrained film-noir look. Semantic states are separated by luminance instead
of hue, so the palette remains truly monochrome while preserving hierarchy.

Every key here must also exist in every other theme file. If you add a new
constant, add it to every theme or the app will break when that theme is
selected.
"""

NAME = "Noir"

CTK_APPEARANCE = "dark"

# Keep the legacy/Tk colour hooks monochrome too. User overrides still win.
THEME_DEFAULTS_OVERRIDE: dict[str, str] = {
    "conflict_higher":    "#a8a8a8",
    "conflict_lower":     "#e2e2e2",
    "plugin_mod":         "#8c8c8c",
    "plugin_separator":   "#8c8c8c",
    "conflict_separator": "#505050",
    "separator_bg":       "#282828",
}

PALETTE: dict[str, str | tuple] = {
    # Backgrounds - deep ink with clearly stepped charcoal surfaces.
    "BG_DEEP":       "#090909",
    "BG_PANEL":      "#141414",
    "BG_HEADER":     "#1c1c1c",
    "BG_ROW":        "#171717",
    "BG_ROW_ALT":    "#1e1e1e",
    "BG_ROW_HOVER":  "#2b2b2b",
    "BG_LIST":       "#101010",
    "BG_SEP":        "#282828",
    "BG_HOVER":      "#303030",
    "BG_SELECT":     "#484848",
    "BG_HOVER_ROW":  "#2b2b2b",

    # Accents - gunmetal silver with pale text for selected/highlighted rows.
    "ACCENT":        "#626262",
    "ACCENT_HOV":    "#707070",
    "TEXT_ON_ACCENT":"#f7f7f7",

    # Text - warm-white hierarchy without pure white glare.
    "TEXT_MAIN":     "#e8e8e8",
    "TEXT_DIM":      "#8a8a8a",
    "TEXT_MUTED":    "#b0b0b0",
    "TEXT_FAINT":    "#707070",
    "TEXT_SEP":      "#c8c8c8",
    "TEXT_WHITE":    "#f7f7f7",
    "TEXT_BLACK":    "#050505",
    "TEXT_OK":       "#c2c2c2",
    "TEXT_ERR":      "#f0f0f0",
    "TEXT_WARN":     "#a0a0a0",
    "TEXT_OK_BRIGHT":   "#d2d2d2",
    "TEXT_ERR_BRIGHT":  "#ffffff",
    "TEXT_WARN_BRIGHT": "#b6b6b6",

    # Borders - thin graphite-to-silver separation.
    "BORDER":        "#3a3a3a",
    "BORDER_DIM":    "#494949",
    "BORDER_FAINT":  "#5c5c5c",

    # Buttons - semantic families use distinct luminance ranges.
    "RED_BTN":       "#c8c8c8",
    "RED_HOV":       "#e0e0e0",
    "BTN_DANGER":        "#c8c8c8",
    "BTN_DANGER_HOV":    "#e2e2e2",
    "BTN_DANGER_ALT":    "#aaaaaa",
    "BTN_DANGER_ALT_HOV":"#c2c2c2",
    "BTN_DANGER_DEEP":   "#8c8c8c",
    "BTN_DANGER_DEEP_HOV":"#a4a4a4",
    "BTN_CANCEL":        "#b8b8b8",
    "BTN_CANCEL_HOV":    "#d0d0d0",

    "BTN_SUCCESS":          "#8e8e8e",
    "BTN_SUCCESS_HOV":      "#a6a6a6",
    "BTN_SUCCESS_ALT":      "#787878",
    "BTN_SUCCESS_ALT_HOV":  "#909090",
    "BTN_SUCCESS_DEEP":     "#626262",
    "BTN_SUCCESS_DEEP_HOV": "#7a7a7a",

    "BTN_WARN":          "#747474",
    "BTN_WARN_HOV":      "#8c8c8c",
    "BTN_WARN_DEEP":     "#5c5c5c",
    "BTN_WARN_DEEP_HOV": "#747474",
    "BTN_WARN_BROWN":    "#4a4a4a",
    "BTN_WARN_BROWN_HOV":"#606060",
    "BTN_WARN_ORANGE":   "#828282",
    "BTN_WARN_ORANGE_HOV":"#9a9a9a",

    "BTN_INFO":          "#565656",
    "BTN_INFO_HOV":      "#6e6e6e",
    "BTN_INFO_DEEP":     "#484848",
    "BTN_INFO_DEEP_HOV": "#606060",
    "BTN_NEUTRAL":       "#626262",
    "BTN_NEUTRAL_HOV":   "#7a7a7a",

    "BTN_GREY":        "#303030",
    "BTN_GREY_HOV":    "#424242",
    "BTN_GREY_ALT":    "#282828",
    "BTN_GREY_ALT_HOV":"#3a3a3a",

    "BTN_PURPLE":     "#707070",
    "BTN_PURPLE_HOV": "#898989",

    # Tree tags - differentiated with luminance, not colour.
    "TAG_FOLDER":       "#d0d0d0",
    "TAG_BSA":          "#aaaaaa",
    "TAG_BSA_ALT":      "#c0c0c0",
    "TAG_INI_PROFILE":  "#e0e0e0",
    "TAG_BUNDLED_FG":   "#bcbcbc",
    "TAG_BUNDLED_BG":   "#252525",
    "TAG_INSTALLED_BG": "#303030",
    "TAG_UNORDERED_FG": "#707070",

    # Tones - an ordered silver scale for semantic indicators.
    "TONE_GREEN":     "#c4c4c4",
    "TONE_RED":       "#f0f0f0",
    "TONE_BLUE":      "#a8a8a8",
    "TONE_CYAN":      "#d4d4d4",
    "TONE_BLUE_SOFT": "#969696",
    "TONE_FLAG":      "#b8b8b8",

    # Scrollbars
    "SCROLL_BG":     "#2e2e2e",
    "SCROLL_TROUGH": "#090909",
    "SCROLL_ACTIVE": "#bcbcbc",

    # Overlays / special
    "BG_OVERLAY_ERR":  "#202020",
    "BG_OVERLAY_DEEP": "#090909",
    "BG_CARD":         "#202020",
    "BG_CARD_ALT":     "#181818",
    "BG_GREEN_ROW":    "#393939",
    "BG_GREEN_DEEP":   "#303030",
    "BG_RED_DEEP":     "#464646",
    "BG_ORANGE_DEEP":  "#383838",
    "BG_GREEN_TEXT":   "#d0d0d0",
    "BG_RED_TEXT":     "#ffffff",
    "BG_ORANGE_TEXT":  "#c0c0c0",
    "BG_BLUE_DEEP":    "#292929",
    "BG_BLUE_TEXT":    "#b0b0b0",
    "BG_DARK_BLUE":    "#1d1d1d",
    "BG_DARK_GREEN":   "#252525",
    "BG_ENTRY":        "#0f0f0f",
    "BG_BTN_SAVE":     "#626262",
    "BG_SELECT_BAR":   "#393939",
    "BG_MOD_REQ":      "#969696",
    "BG_MOD_OPT":      "#686868",

    # Status
    "STATUS_ERR_BRIGHT":    "#ffffff",
    "STATUS_BADGE_RED":     "#e0e0e0",
    "STATUS_BADGE_GREEN":   "#a0a0a0",
    "STATUS_SUCCESS_SOLID": "#c8c8c8",
    "STATUS_QUEUED":        "#b4b4b4",
    "STATUS_DL_GREEN":      "#a8a8a8",

    # Card text
    "TEXT_CARD":     "#dddddd",
    "TEXT_CARD_DIM": "#747474",
    "TEXT_CARD_MED": "#eeeeee",
    "TEXT_TREE_FG":  "#c2c2c2",

    # CTk light/dark tuples - dark values follow the same ink/silver scale.
    "CTK_TEXT":       ("#050505", "#f7f7f7"),
    "CTK_FOOTER_FG":  ("#eeeeee", "#1c1c1c"),
    "CTK_FOOTER_HOV": ("#dedede", "#2b2b2b"),
    "CTK_SEP":        ("#c8c8c8", "#3a3a3a"),
    "CTK_SEP_ALT":    ("#d0d0d0", "#484848"),
    "CTK_BTN_HOVER":  ("gray90", "gray20"),

    # Dropdown / combobox arrow glyph
    "DROPDOWN_ARROW": "#d8d8d8",

    # Misc
    "LINK_BLUE":     "#d0d0d0",

    # Plugin-cycle status rows
    "PLUGIN_CYCLE_ERR_BG":  "#464646",
    "PLUGIN_CYCLE_ERR_FG":  "#ffffff",
    "PLUGIN_CYCLE_OK_BG":   "#303030",
    "PLUGIN_CYCLE_OK_FG":   "#d0d0d0",
    "PLUGIN_CYCLE_WARN_BG": "#383838",
    "PLUGIN_CYCLE_WARN_FG": "#c0c0c0",
    "PLUGIN_CYCLE_ANCHOR":  "#b4b4b4",
    "PLUGIN_CYCLE_LINK":    "#d0d0d0",

    # File conflict states
    "FILE_WIN":      "#b0b0b0",
    "FILE_LOSE":     "#f0f0f0",
    "FILE_DIM":      "#686868",
    "FILE_ANCHOR":   "#8c8c8c",

    # Drag selection outline
    "HIGHLIGHT_DRAG": "#f2f2f2",

    # Cross-panel conflict row highlights
    "CONFLICT_HL_WIN":    "#4c4c4c",
    "CONFLICT_HL_LOSE":   "#6a6a6a",
    "CONFLICT_HL_ANCHOR": "#5a5a5a",
    "REQ_HL_REQUIRES":    "#404040",
    "REQ_HL_REQUIRED_BY": "#343434",

    # Framework-status banner rows
    "FRAMEWORK_INSTALLED_BG": "#303030", "FRAMEWORK_INSTALLED_FG": "#d0d0d0",
    "FRAMEWORK_STAGED_BG":    "#383838", "FRAMEWORK_STAGED_FG":    "#c0c0c0",
    "FRAMEWORK_DISABLED_BG":  "#292929", "FRAMEWORK_DISABLED_FG":  "#b0b0b0",
    "FRAMEWORK_MISSING_BG":   "#464646", "FRAMEWORK_MISSING_FG":   "#ffffff",

    # Modlist boundary separator bands
    "OVERWRITE_SEP_BG": "#252525", "OVERWRITE_SEP_FG": "#d0d0d0",
    "ROOT_SEP_BG":      "#1d1d1d", "ROOT_SEP_FG":      "#b0b0b0",

    # Checkbox fill when checked
    "CHECK_FILL": "#626262",
}
