"""Catppuccin Mocha - a soft, low-contrast pastel dark theme.

The palette follows the official Catppuccin Mocha spec: muted base surfaces
in the "crust/mantle/base" ramp, the lavender/mauve accent family, and the
pastel semantic colours.  Every key mirrors dark.py so the theme covers the
entire application.
"""

NAME = "Catppuccin Mocha"

CTK_APPEARANCE = "dark"

# Theme-aware defaults for legacy/Tk conflict and separator colour hooks.
THEME_DEFAULTS_OVERRIDE: dict[str, str] = {
    "conflict_higher":    "#2c4a3e",
    "conflict_lower":     "#5c3646",
    "plugin_mod":         "#6b4a3a",
    "conflict_separator": "#45475a",
    "plugin_separator":   "#6b4a3a",
    "separator_bg":       "#313244",
}

PALETTE: dict[str, str | tuple] = {
    # Backgrounds - the canonical Mocha crust/mantle/base/surface ramp.
    "BG_DEEP":       "#11111b",
    "BG_PANEL":      "#181825",
    "BG_HEADER":     "#1e1e2e",
    "BG_ROW":        "#1e1e2e",
    "BG_ROW_ALT":    "#313244",
    "BG_ROW_HOVER":  "#45475a",
    "BG_LIST":       "#181825",
    "BG_SEP":        "#313244",
    "BG_HOVER":      "#3a3a54",
    "BG_SELECT":     "#b4befe",
    "BG_HOVER_ROW":  "#45475a",

    # Accents - Mocha lavender, the signature Catppuccin highlight.
    "ACCENT":        "#b4befe",
    "ACCENT_HOV":    "#c7d0ff",
    "TEXT_ON_ACCENT":"#1e1e2e",

    # Text - the subtext/overlay ramp on Mocha text white.
    "TEXT_MAIN":     "#cdd6f4",
    "TEXT_DIM":      "#7f849c",
    "TEXT_MUTED":    "#a6adc8",
    "TEXT_FAINT":    "#6c7086",
    "TEXT_SEP":      "#bac2de",
    "TEXT_WHITE":    "#ffffff",
    "TEXT_BLACK":    "#11111b",
    "TEXT_OK":       "#a6e3a1",
    "TEXT_ERR":      "#f38ba8",
    "TEXT_WARN":     "#f9e2af",
    "TEXT_OK_BRIGHT":   "#b9edb4",
    "TEXT_ERR_BRIGHT":  "#f7a2bb",
    "TEXT_WARN_BRIGHT": "#fbebc4",

    # Borders - the surface1/surface2 divisions.
    "BORDER":        "#45475a",
    "BORDER_DIM":    "#585b70",
    "BORDER_FAINT":  "#6c7086",

    # Buttons - reds.
    "RED_BTN":       "#e0596f",
    "RED_HOV":       "#f38ba8",
    "BTN_DANGER":        "#e0596f",
    "BTN_DANGER_HOV":    "#f38ba8",
    "BTN_DANGER_ALT":    "#b84a5f",
    "BTN_DANGER_ALT_HOV":"#d05a71",
    "BTN_DANGER_DEEP":   "#8f3a4b",
    "BTN_DANGER_DEEP_HOV":"#ab4559",
    "BTN_CANCEL":        "#c85a70",
    "BTN_CANCEL_HOV":    "#dd6c83",

    # Buttons - greens.
    "BTN_SUCCESS":          "#5a9c62",
    "BTN_SUCCESS_HOV":      "#74bd79",
    "BTN_SUCCESS_ALT":      "#478053",
    "BTN_SUCCESS_ALT_HOV":  "#5a9c62",
    "BTN_SUCCESS_DEEP":     "#38653f",
    "BTN_SUCCESS_DEEP_HOV": "#478053",

    # Buttons - oranges (Mocha peach family).
    "BTN_WARN":          "#d08b52",
    "BTN_WARN_HOV":      "#fab387",
    "BTN_WARN_DEEP":     "#9c6640",
    "BTN_WARN_DEEP_HOV": "#b87a4c",
    "BTN_WARN_BROWN":    "#7a5236",
    "BTN_WARN_BROWN_HOV":"#946440",
    "BTN_WARN_ORANGE":   "#d47f4e",
    "BTN_WARN_ORANGE_HOV":"#ea9662",

    # Buttons - blues.
    "BTN_INFO":          "#5a7fc4",
    "BTN_INFO_HOV":      "#89b4fa",
    "BTN_INFO_DEEP":     "#45639e",
    "BTN_INFO_DEEP_HOV": "#5a7fc4",
    "BTN_NEUTRAL":       "#585b70",
    "BTN_NEUTRAL_HOV":   "#6c7086",

    # Buttons - surface neutrals.
    "BTN_GREY":        "#45475a",
    "BTN_GREY_HOV":    "#585b70",
    "BTN_GREY_ALT":    "#313244",
    "BTN_GREY_ALT_HOV":"#45475a",

    # Buttons - purples (Mocha mauve).
    "BTN_PURPLE":     "#a276d6",
    "BTN_PURPLE_HOV": "#cba6f7",

    # Tree tags.
    "TAG_FOLDER":       "#89dceb",
    "TAG_BSA":          "#f9e2af",
    "TAG_BSA_ALT":      "#94e2d5",
    "TAG_INI_PROFILE":  "#94e2d5",
    "TAG_BUNDLED_FG":   "#89b4fa",
    "TAG_BUNDLED_BG":   "#25304d",
    "TAG_INSTALLED_BG": "#25382f",
    "TAG_UNORDERED_FG": "#6c7086",

    # Tones.
    "TONE_GREEN":     "#a6e3a1",
    "TONE_RED":       "#f38ba8",
    "TONE_BLUE":      "#89b4fa",
    "TONE_CYAN":      "#89dceb",
    "TONE_BLUE_SOFT": "#b4befe",
    "TONE_FLAG":      "#f9e2af",

    # Scrollbars.
    "SCROLL_BG":     "#313244",
    "SCROLL_TROUGH": "#181825",
    "SCROLL_ACTIVE": "#b4befe",

    # Overlays / special surfaces.
    "BG_OVERLAY_ERR":  "#3a2230",
    "BG_OVERLAY_DEEP": "#11111b",
    "BG_CARD":         "#1e1e2e",
    "BG_CARD_ALT":     "#181825",
    "BG_GREEN_ROW":    "#25382f",
    "BG_GREEN_DEEP":   "#1f3029",
    "BG_RED_DEEP":     "#3d2531",
    "BG_ORANGE_DEEP":  "#3f3126",
    "BG_GREEN_TEXT":   "#b9edb4",
    "BG_RED_TEXT":     "#f7a2bb",
    "BG_ORANGE_TEXT":  "#fbebc4",
    "BG_BLUE_DEEP":    "#25304d",
    "BG_BLUE_TEXT":    "#a9c3ff",
    "BG_DARK_BLUE":    "#1c2338",
    "BG_DARK_GREEN":   "#1c2b26",
    "BG_ENTRY":        "#181825",
    "BG_BTN_SAVE":     "#5a7fc4",
    "BG_SELECT_BAR":   "#3a3a54",
    "BG_MOD_REQ":      "#5a9c62",
    "BG_MOD_OPT":      "#d08b52",

    # Status colours.
    "STATUS_ERR_BRIGHT":    "#f7a2bb",
    "STATUS_BADGE_RED":     "#e0596f",
    "STATUS_BADGE_GREEN":   "#6ab06a",
    "STATUS_SUCCESS_SOLID": "#a6e3a1",
    "STATUS_QUEUED":        "#fab387",
    "STATUS_DL_GREEN":      "#94e2d5",

    # Card text.
    "TEXT_CARD":     "#cdd6f4",
    "TEXT_CARD_DIM": "#7f849c",
    "TEXT_CARD_MED": "#a6adc8",
    "TEXT_TREE_FG":  "#a6e3a1",

    # CTk light/dark tuples.
    "CTK_TEXT":       ("#1e1e2e", "#cdd6f4"),
    "CTK_FOOTER_FG":  ("#e6e9f0", "#1e1e2e"),
    "CTK_FOOTER_HOV": ("#dcdfea", "#313244"),
    "CTK_SEP":        ("#c9ccd6", "#45475a"),
    "CTK_SEP_ALT":    ("#d0d0d0", "#585b70"),
    "CTK_BTN_HOVER":  ("gray90", "#313244"),

    # Dropdown / combobox arrow glyph.
    "DROPDOWN_ARROW": "#b4befe",

    # Links.
    "LINK_BLUE":     "#89dceb",

    # Plugin-cycle status rows.
    "PLUGIN_CYCLE_ERR_BG":  "#3d2531",
    "PLUGIN_CYCLE_ERR_FG":  "#f7a2bb",
    "PLUGIN_CYCLE_OK_BG":   "#25382f",
    "PLUGIN_CYCLE_OK_FG":   "#b9edb4",
    "PLUGIN_CYCLE_WARN_BG": "#3f3126",
    "PLUGIN_CYCLE_WARN_FG": "#fbebc4",
    "PLUGIN_CYCLE_ANCHOR":  "#fab387",
    "PLUGIN_CYCLE_LINK":    "#89dceb",

    # File conflict states.
    "FILE_WIN":      "#5a9c62",
    "FILE_LOSE":     "#b84a5f",
    "FILE_DIM":      "#6c7086",
    "FILE_ANCHOR":   "#b87a4c",

    # Drag selection outline.
    "HIGHLIGHT_DRAG": "#89b4fa",

    # Cross-panel conflict row highlights.
    "CONFLICT_HL_WIN":    "#2c4a3e",
    "CONFLICT_HL_LOSE":   "#5c3646",
    "CONFLICT_HL_ANCHOR": "#6b4a3a",
    "REQ_HL_REQUIRES":    "#4a3a6b",
    "REQ_HL_REQUIRED_BY": "#2e4470",

    # Framework-status banner rows.
    "FRAMEWORK_INSTALLED_BG": "#25382f", "FRAMEWORK_INSTALLED_FG": "#b9edb4",
    "FRAMEWORK_STAGED_BG":    "#3f3126", "FRAMEWORK_STAGED_FG":    "#fbebc4",
    "FRAMEWORK_DISABLED_BG":  "#25304d", "FRAMEWORK_DISABLED_FG":  "#a9c3ff",
    "FRAMEWORK_MISSING_BG":   "#3d2531", "FRAMEWORK_MISSING_FG":   "#f7a2bb",

    # Modlist boundary separator bands.
    "OVERWRITE_SEP_BG": "#1c2b26", "OVERWRITE_SEP_FG": "#a6e3a1",
    "ROOT_SEP_BG":      "#1c2338", "ROOT_SEP_FG":      "#89b4fa",

    # Checkbox fill when checked.
    "CHECK_FILL": "#b4befe",
}
