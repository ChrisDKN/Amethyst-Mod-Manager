"""Stardew - a warm, colourful countryside theme.

The palette draws from Stardew Valley's title art: parchment cream, timber
copper, deep country blue, clear-sky blue, and leaf green. It keeps the app's
semantic states distinct while giving panels and lists a sunlit, hand-painted
feel.

Every key here must also exist in every other theme file. If you add a new
constant, add it to every theme or the app will break when that theme is
selected.
"""

NAME = "Stardew"

CTK_APPEARANCE = "light"

# Theme-aware defaults for legacy/Tk conflict and separator colour hooks.
THEME_DEFAULTS_OVERRIDE: dict[str, str] = {
    "conflict_higher":    "#477e35",
    "conflict_lower":     "#a84a34",
    "plugin_mod":         "#94502e",
    "plugin_separator":   "#94502e",
    "conflict_separator": "#9f5b34",
    "separator_bg":       "#8e4529",
}

PALETTE: dict[str, str | tuple] = {
    # Backgrounds - pale aqua framing warm parchment and timber surfaces.
    "BG_DEEP":       "#d6ece9",
    "BG_PANEL":      "#f1d293",
    "BG_HEADER":     "#e3aa67",
    "BG_ROW":        "#fff1c4",
    "BG_ROW_ALT":    "#f5dda3",
    "BG_ROW_HOVER":  "#e8c17d",
    "BG_LIST":       "#f8e5b0",
    "BG_SEP":        "#8e4529",
    "BG_HOVER":      "#b9ddf5",
    "BG_SELECT":     "#1e659e",
    "BG_HOVER_ROW":  "#e8c17d",

    # Accents - country blue from the sky behind the wooden title board.
    "ACCENT":        "#236fa9",
    "ACCENT_HOV":    "#195b91",
    "TEXT_ON_ACCENT":"#fff8e7",

    # Text - deep navy and earthy browns on the parchment surfaces.
    "TEXT_MAIN":     "#173e68",
    "TEXT_DIM":      "#65594b",
    "TEXT_MUTED":    "#765b43",
    "TEXT_FAINT":    "#8c755f",
    "TEXT_SEP":      "#fff0c6",
    "TEXT_WHITE":    "#fff8e7",
    "TEXT_BLACK":    "#1b2c36",
    "TEXT_OK":       "#376f2a",
    "TEXT_ERR":      "#9d3f2e",
    "TEXT_WARN":     "#805415",
    "TEXT_OK_BRIGHT":   "#2f7025",
    "TEXT_ERR_BRIGHT":  "#a43c2b",
    "TEXT_WARN_BRIGHT": "#8a5914",

    # Borders - the copper and dark wood outlines of the title board.
    "BORDER":        "#9f5b34",
    "BORDER_DIM":    "#c17b47",
    "BORDER_FAINT":  "#dba363",

    # Buttons - earthy semantic families with clear hover steps.
    "RED_BTN":       "#b65436",
    "RED_HOV":       "#9a3e29",
    "BTN_DANGER":        "#b65436",
    "BTN_DANGER_HOV":    "#9a3e29",
    "BTN_DANGER_ALT":    "#a14931",
    "BTN_DANGER_ALT_HOV":"#873824",
    "BTN_DANGER_DEEP":   "#843622",
    "BTN_DANGER_DEEP_HOV":"#6f2d1d",
    "BTN_CANCEL":        "#a84a34",
    "BTN_CANCEL_HOV":    "#8d3928",

    "BTN_SUCCESS":          "#4b8f32",
    "BTN_SUCCESS_HOV":      "#3c7729",
    "BTN_SUCCESS_ALT":      "#417e2e",
    "BTN_SUCCESS_ALT_HOV":  "#346725",
    "BTN_SUCCESS_DEEP":     "#376b29",
    "BTN_SUCCESS_DEEP_HOV": "#2d5822",

    "BTN_WARN":          "#d58a2d",
    "BTN_WARN_HOV":      "#bd6f1e",
    "BTN_WARN_DEEP":     "#a96620",
    "BTN_WARN_DEEP_HOV": "#8d5219",
    "BTN_WARN_BROWN":    "#84502b",
    "BTN_WARN_BROWN_HOV":"#6e4022",
    "BTN_WARN_ORANGE":   "#c9702c",
    "BTN_WARN_ORANGE_HOV":"#aa5821",

    "BTN_INFO":          "#287fc5",
    "BTN_INFO_HOV":      "#1d69a6",
    "BTN_INFO_DEEP":     "#1e6098",
    "BTN_INFO_DEEP_HOV": "#184f7e",
    "BTN_NEUTRAL":       "#315f8c",
    "BTN_NEUTRAL_HOV":   "#264f77",

    "BTN_GREY":        "#b18a62",
    "BTN_GREY_HOV":    "#9b7551",
    "BTN_GREY_ALT":    "#c49a69",
    "BTN_GREY_ALT_HOV":"#aa8057",

    "BTN_PURPLE":     "#806383",
    "BTN_PURPLE_HOV": "#684f6d",

    # Tree tags
    "TAG_FOLDER":       "#1767a2",
    "TAG_BSA":          "#9a552d",
    "TAG_BSA_ALT":      "#2789c9",
    "TAG_INI_PROFILE":  "#397f2d",
    "TAG_BUNDLED_FG":   "#1b5b91",
    "TAG_BUNDLED_BG":   "#c8e3f3",
    "TAG_INSTALLED_BG": "#d2e7b7",
    "TAG_UNORDERED_FG": "#8c755f",

    # Tones
    "TONE_GREEN":     "#438b31",
    "TONE_RED":       "#b65436",
    "TONE_BLUE":      "#288ee5",
    "TONE_CYAN":      "#43a9bd",
    "TONE_BLUE_SOFT": "#6aaee0",
    "TONE_FLAG":      "#d58a2d",

    # Scrollbars
    "SCROLL_BG":     "#d8ae73",
    "SCROLL_TROUGH": "#f8e5b0",
    "SCROLL_ACTIVE": "#287fc5",

    # Overlays / special
    "BG_OVERLAY_ERR":  "#efd0b4",
    "BG_OVERLAY_DEEP": "#d6ece9",
    "BG_CARD":         "#ffeabd",
    "BG_CARD_ALT":     "#f4d79c",
    "BG_GREEN_ROW":    "#d2e7b7",
    "BG_GREEN_DEEP":   "#c4dda6",
    "BG_RED_DEEP":     "#efc6ad",
    "BG_ORANGE_DEEP":  "#f3d49a",
    "BG_GREEN_TEXT":   "#2e6223",
    "BG_RED_TEXT":     "#873824",
    "BG_ORANGE_TEXT":  "#775017",
    "BG_BLUE_DEEP":    "#c8e3f3",
    "BG_BLUE_TEXT":    "#174f7d",
    "BG_DARK_BLUE":    "#b9d9ed",
    "BG_DARK_GREEN":   "#c9dfad",
    "BG_ENTRY":        "#fff5d5",
    "BG_BTN_SAVE":     "#236fa9",
    "BG_SELECT_BAR":   "#b8d9ed",
    "BG_MOD_REQ":      "#4b8f32",
    "BG_MOD_OPT":      "#d58a2d",

    # Status
    "STATUS_ERR_BRIGHT":    "#a43c2b",
    "STATUS_BADGE_RED":     "#b65436",
    "STATUS_BADGE_GREEN":   "#4b8f32",
    "STATUS_SUCCESS_SOLID": "#3c8b2a",
    "STATUS_QUEUED":        "#a96620",
    "STATUS_DL_GREEN":      "#438b31",

    # Card text
    "TEXT_CARD":     "#26486a",
    "TEXT_CARD_DIM": "#765b43",
    "TEXT_CARD_MED": "#173e68",
    "TEXT_TREE_FG":  "#376f2a",

    # CTk light/dark tuples
    "CTK_TEXT":       ("#173e68", "#fff8e7"),
    "CTK_FOOTER_FG":  ("#e3aa67", "#173e68"),
    "CTK_FOOTER_HOV": ("#d59555", "#235d91"),
    "CTK_SEP":        ("#9f5b34", "#8e4529"),
    "CTK_SEP_ALT":    ("#c17b47", "#a45a32"),
    "CTK_BTN_HOVER":  ("#e8c17d", "#235d91"),

    # Dropdown / combobox arrow glyph
    "DROPDOWN_ARROW": "#173e68",

    # Misc
    "LINK_BLUE":     "#155b94",

    # Plugin-cycle status rows
    "PLUGIN_CYCLE_ERR_BG":  "#efc6ad",
    "PLUGIN_CYCLE_ERR_FG":  "#873824",
    "PLUGIN_CYCLE_OK_BG":   "#d2e7b7",
    "PLUGIN_CYCLE_OK_FG":   "#2e6223",
    "PLUGIN_CYCLE_WARN_BG": "#f3d49a",
    "PLUGIN_CYCLE_WARN_FG": "#775017",
    "PLUGIN_CYCLE_ANCHOR":  "#94502e",
    "PLUGIN_CYCLE_LINK":    "#155b94",

    # File conflict states
    "FILE_WIN":      "#397f2d",
    "FILE_LOSE":     "#a43c2b",
    "FILE_DIM":      "#8c755f",
    "FILE_ANCHOR":   "#94502e",

    # Drag selection outline
    "HIGHLIGHT_DRAG": "#1e659e",

    # Cross-panel conflict row highlights - dark enough for selected-row text.
    "CONFLICT_HL_WIN":    "#477e35",
    "CONFLICT_HL_LOSE":   "#a84a34",
    "CONFLICT_HL_ANCHOR": "#94502e",
    "REQ_HL_REQUIRES":    "#6d517f",
    "REQ_HL_REQUIRED_BY": "#275c88",

    # Framework-status banner rows
    "FRAMEWORK_INSTALLED_BG": "#d2e7b7", "FRAMEWORK_INSTALLED_FG": "#2e6223",
    "FRAMEWORK_STAGED_BG":    "#f3d49a", "FRAMEWORK_STAGED_FG":    "#775017",
    "FRAMEWORK_DISABLED_BG":  "#c8e3f3", "FRAMEWORK_DISABLED_FG":  "#174f7d",
    "FRAMEWORK_MISSING_BG":   "#efc6ad", "FRAMEWORK_MISSING_FG":   "#873824",

    # Modlist boundary separator bands
    "OVERWRITE_SEP_BG": "#c9dfad", "OVERWRITE_SEP_FG": "#2e6223",
    "ROOT_SEP_BG":      "#b9d9ed", "ROOT_SEP_FG":      "#174f7d",

    # Checkbox fill when checked
    "CHECK_FILL": "#236fa9",
}
