"""
Breeze Light theme — matches the KDE Breeze Dark colour scheme.

Every key here must also exist in every other theme file. If you add a new
constant, add it to every theme or the app will break when that theme is
selected.
"""

NAME = "Breeze Light"

CTK_APPEARANCE = "light"

PALETTE: dict[str, str | tuple] = {
    # Backgrounds — Breeze "window" / "view" / "button" greys.
    "BG_DEEP":       "#dee0e2",   # window background
    "BG_PANEL":      "#eff0f1",   # raised panel
    "BG_HEADER":     "#dee0e2",   # toolbar / header / button base
    "BG_ROW":        "#fcfcfc",
    "BG_ROW_ALT":    "#f7f7f7",   # zebra alt
    "BG_ROW_HOVER":  "#e3e5e7",
    "BG_LIST":       "#e0dfde",   # Breeze "view" background (lists/trees)
    "BG_SEP":        "#e0dfde",
    "BG_HOVER":      "#c2e0f5",
    "BG_SELECT":     "#3daee9",   # Breeze selection blue
    "BG_HOVER_ROW":  "#e3e5e7",

    # Accents — Breeze blue.
    "ACCENT":        "#3daee9",
    "ACCENT_HOV":    "#2a8ac7",
    "TEXT_ON_ACCENT":"#ffffff",

    # Text — Breeze foreground greys.
    "TEXT_MAIN":     "#232627",
    "TEXT_DIM":      "#4d4d4d",
    "TEXT_MUTED":    "#6a6a6a",
    "TEXT_FAINT":    "#898989",
    "TEXT_SEP":      "#4d4d4d",
    "TEXT_WHITE":    "#ffffff",
    "TEXT_BLACK":    "#000000",
    "TEXT_OK":       "#009665",
    "TEXT_ERR":      "#da4453",
    "TEXT_WARN":     "#ffb81c",
    "TEXT_OK_BRIGHT":   "#00b87b",
    "TEXT_ERR_BRIGHT":  "#e55f70",
    "TEXT_WARN_BRIGHT": "#ffc947",

    # Borders — subtle Breeze separators.
    "BORDER":        "#dfdfdf",
    "BORDER_DIM":    "#e8e8e8",
    "BORDER_FAINT":  "#cbcdcd",

    # Buttons — reds
    "RED_BTN":       "#da4453",
    "RED_HOV":       "#c72637",
    "BTN_DANGER":        "#da4453",
    "BTN_DANGER_HOV":    "#c72637",
    "BTN_DANGER_ALT":    "#c72a37",
    "BTN_DANGER_ALT_HOV":"#a6232e",
    "BTN_DANGER_DEEP":   "#8f1b24",
    "BTN_DANGER_DEEP_HOV":"#7a171f",
    "BTN_CANCEL":        "#c72637",
    "BTN_CANCEL_HOV":    "#a6232e",

    # Buttons — greens
    "BTN_SUCCESS":          "#009665",
    "BTN_SUCCESS_HOV":      "#007d54",
    "BTN_SUCCESS_ALT":      "#006f4a",
    "BTN_SUCCESS_ALT_HOV":  "#005c3d",
    "BTN_SUCCESS_DEEP":     "#004a31",
    "BTN_SUCCESS_DEEP_HOV": "#003a26",

    # Buttons — oranges
    "BTN_WARN":          "#ffb81c",
    "BTN_WARN_HOV":      "#e6a619",
    "BTN_WARN_DEEP":     "#cc9416",
    "BTN_WARN_DEEP_HOV": "#b08013",
    "BTN_WARN_BROWN":    "#8f6610",
    "BTN_WARN_BROWN_HOV":"#75530d",
    "BTN_WARN_ORANGE":   "#cc7a00",
    "BTN_WARN_ORANGE_HOV":"#b06900",

    # Buttons — blues
    "BTN_INFO":          "#3daee9",
    "BTN_INFO_HOV":      "#2a8ac7",
    "BTN_INFO_DEEP":     "#2172a3",
    "BTN_INFO_DEEP_HOV": "#1a5980",
    "BTN_NEUTRAL":       "#586a7a",
    "BTN_NEUTRAL_HOV":   "#657a8f",

    # Buttons — greys
    "BTN_GREY":        "#c6c9cb",
    "BTN_GREY_HOV":    "#cbced0",
    "BTN_GREY_ALT":    "#d6d5d4",
    "BTN_GREY_ALT_HOV":"#dadad9",

    # Buttons — purples
    "BTN_PURPLE":     "#9b59b6",
    "BTN_PURPLE_HOV": "#8e4bad",

    # Tree tags
    "TAG_FOLDER":       "#3daee9",
    "TAG_BSA":          "#ffb81c",
    "TAG_BSA_ALT":      "#5cd8e5",
    "TAG_INI_PROFILE":  "#009665",
    "TAG_BUNDLED_FG":   "#3daee9",
    "TAG_BUNDLED_BG":   "#dae7f3",
    "TAG_INSTALLED_BG": "#d4edda",
    "TAG_UNORDERED_FG": "#6a6a6a",

    # Tones
    "TONE_GREEN":     "#009665",
    "TONE_RED":       "#da4453",
    "TONE_BLUE":      "#3daee9",
    "TONE_CYAN":      "#009665",
    "TONE_BLUE_SOFT": "#6ebcf0",
    "TONE_FLAG":      "#ffb81c",

    # Scrollbars
    "SCROLL_BG":     "#dfdfdf",
    "SCROLL_TROUGH": "#eff0f1",
    "SCROLL_ACTIVE": "#3daee9",

    # Overlays / special
    "BG_OVERLAY_ERR":  "#fadddf",
    "BG_OVERLAY_DEEP": "#eff0f1",
    "BG_CARD":         "#ffffff",
    "BG_CARD_ALT":     "#fcfcfc",
    "BG_GREEN_ROW":    "#d4edda",
    "BG_GREEN_DEEP":   "#c3e6cb",
    "BG_RED_DEEP":     "#fadddf",
    "BG_ORANGE_DEEP":  "#fcebd5",
    "BG_GREEN_TEXT":   "#0b6847",
    "BG_RED_TEXT":     "#8c2b35",
    "BG_ORANGE_TEXT":  "#856404",
    "BG_BLUE_DEEP":    "#dae7f3",
    "BG_BLUE_TEXT":    "#2172a3",
    "BG_DARK_BLUE":    "#dae7f3",
    "BG_DARK_GREEN":   "#d4edda",
    "BG_ENTRY":        "#ffffff",
    "BG_BTN_SAVE":     "#3daee9",
    "BG_SELECT_BAR":   "#dae7f3",
    "BG_MOD_REQ":      "#009665",
    "BG_MOD_OPT":      "#ffb81c",

    # Status
    "STATUS_ERR_BRIGHT":    "#e55f70",
    "STATUS_BADGE_RED":     "#da4453",
    "STATUS_BADGE_GREEN":   "#009665",
    "STATUS_SUCCESS_SOLID": "#009665",
    "STATUS_QUEUED":        "#ffb81c",
    "STATUS_DL_GREEN":      "#009665",

    # Card text
    "TEXT_CARD":     "#232627",
    "TEXT_CARD_DIM": "#4d4d4d",
    "TEXT_CARD_MED": "#3a3a3a",
    "TEXT_TREE_FG":  "#009665",

    # CTk light/dark tuples
    "CTK_TEXT":       ["#232627", "#fcfcfc"],
    "CTK_FOOTER_FG":  ["#4d4d4d", "#fcfcfc"],
    "CTK_FOOTER_HOV": ["#232627", "#eff0f1"],
    "CTK_SEP":        ["#dfdfdf", "#3a3a3a"],
    "CTK_SEP_ALT":    ["#e8e8e8", "#444444"],
    "CTK_BTN_HOVER":  ["gray90", "gray25"],

    # Dropdown / combobox arrow glyph (tinted via QSS-generated PNG)
    "DROPDOWN_ARROW": "#3daee9",

    # Misc
    "LINK_BLUE":     "#3daee9",

    # Plugin-cycle status rows (Show Cycle view)
    "PLUGIN_CYCLE_ERR_BG":  "#fadddf",
    "PLUGIN_CYCLE_ERR_FG":  "#8c2b35",
    "PLUGIN_CYCLE_OK_BG":   "#d4edda",
    "PLUGIN_CYCLE_OK_FG":   "#0b6847",
    "PLUGIN_CYCLE_WARN_BG": "#fcebd5",
    "PLUGIN_CYCLE_WARN_FG": "#856404",
    "PLUGIN_CYCLE_ANCHOR":  "#b86900",
    "PLUGIN_CYCLE_LINK":    "#3daee9",

    # File conflict states (Data / Mod Files / plugin conflicts)
    "FILE_WIN":      "#009665",
    "FILE_LOSE":     "#da4453",
    "FILE_DIM":      "#6a6a6a",
    "FILE_ANCHOR":   "#b86900",

    # Drag selection outline (modlist / plugins)
    "HIGHLIGHT_DRAG": "#3daee9",

    # Cross-panel conflict row highlights (modlist / plugins / data tree)
    "CONFLICT_HL_WIN":    "#009665",   # selection beats this mod (green)
    "CONFLICT_HL_LOSE":   "#da4453",   # this mod beats selection (red)
    "CONFLICT_HL_ANCHOR": "#b86900",   # plugin-selected / anchor mod (orange)
    "REQ_HL_REQUIRES":    "#9b59b6",   # mods the selection requires (purple)
    "REQ_HL_REQUIRED_BY": "#2172a3",   # mods that require the selection (blue)

    # Framework-status banner rows (Plugins tab) — per install state
    "FRAMEWORK_INSTALLED_BG": "#d4edda", "FRAMEWORK_INSTALLED_FG": "#0b6847",
    "FRAMEWORK_STAGED_BG":    "#fcebd5", "FRAMEWORK_STAGED_FG":    "#856404",
    "FRAMEWORK_DISABLED_BG":  "#dae7f3", "FRAMEWORK_DISABLED_FG":  "#2172a3",
    "FRAMEWORK_MISSING_BG":   "#fadddf", "FRAMEWORK_MISSING_FG":   "#8c2b35",

    # Modlist boundary separator bands (pinned Overwrite / Root Folder rows)
    "OVERWRITE_SEP_BG": "#d4edda", "OVERWRITE_SEP_FG": "#009665",
    "ROOT_SEP_BG":      "#dae7f3", "ROOT_SEP_FG":      "#2172a3",

    # Checkbox fill when checked (tick auto-contrasts off this)
    "CHECK_FILL": "#3daee9",
}
