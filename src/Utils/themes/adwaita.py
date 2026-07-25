"""
Adwaita theme

Every key here must also exist in every other theme file. If you add a new
constant, add it to every theme or the app will break when that theme is
selected.
"""

NAME = "Adwaita"

CTK_APPEARANCE = "light"

PALETTE: dict[str, str | tuple] = {
    # Backgrounds — Breeze "window" / "view" / "button" greys.
    "BG_DEEP":       "#fafafb",   # window background
    "BG_PANEL":      "#ebebed",   # raised panel
    "BG_HEADER":     "#fafafb",   # toolbar / header / button base
    "BG_ROW":        "#ffffff",
    "BG_ROW_ALT":    "#f3f3f5",   # zebra alt
    "BG_ROW_HOVER":  "#d3d3d5",
    "BG_LIST":       "#f0f0f0",   # Breeze "view" background (lists/trees)
    "BG_SEP":        "#deddda",
    "BG_HOVER":      "#d3d3d5",
    "BG_SELECT":     "#3584e4",   # Breeze selection blue
    "BG_HOVER_ROW":  "#ffffff",

    # Accents — Breeze blue.
    "ACCENT":        "#3584e4",
    "ACCENT_HOV":    "#1c71d8",
    "TEXT_ON_ACCENT":"#ffffff",

    # Text — Breeze foreground greys.
    "TEXT_MAIN":     "#262626",
    "TEXT_DIM":      "#5e5c64",
    "TEXT_MUTED":    "#77767b",
    "TEXT_FAINT":    "#9a999a",
    "TEXT_SEP":      "#241f31",
    "TEXT_WHITE":    "#ffffff",
    "TEXT_BLACK":    "#000000",
    "TEXT_OK":       "#26a269",
    "TEXT_ERR":      "#e01b24",
    "TEXT_WARN":     "#e5a50a",
    "TEXT_OK_BRIGHT":   "#26a269",
    "TEXT_ERR_BRIGHT":  "#e01b24",
    "TEXT_WARN_BRIGHT": "#e5a50a",

    # Borders — subtle Breeze separators.
    "BORDER":        "#c0bfbc",
    "BORDER_DIM":    "#d5d4d0",
    "BORDER_FAINT":  "#e4e3e2",

    # Buttons — reds
    "RED_BTN":       "#e01b24",
    "RED_HOV":       "#c01c28",
    "BTN_DANGER":        "#e01b24",
    "BTN_DANGER_HOV":    "#c01c28",
    "BTN_DANGER_ALT":    "#be2a2a",
    "BTN_DANGER_ALT_HOV":"#a51d2d",
    "BTN_DANGER_DEEP":   "#9a1627",
    "BTN_DANGER_DEEP_HOV":"#841321",
    "BTN_CANCEL":        "#a51d2d",
    "BTN_CANCEL_HOV":    "#8c1a24",

    # Buttons — greens
    "BTN_SUCCESS":          "#26a269",
    "BTN_SUCCESS_HOV":      "#229360",
    "BTN_SUCCESS_ALT":      "#1d7d52",
    "BTN_SUCCESS_ALT_HOV":  "#196946",
    "BTN_SUCCESS_DEEP":     "#15583a",
    "BTN_SUCCESS_DEEP_HOV": "#114930",

    # Buttons — oranges
    "BTN_WARN":          "#e5a50a",
    "BTN_WARN_HOV":      "#ce9309",
    "BTN_WARN_DEEP":     "#ad7c08",
    "BTN_WARN_DEEP_HOV": "#8f6507",
    "BTN_WARN_BROWN":    "#7a5705",
    "BTN_WARN_BROWN_HOV":"#5e4204",
    "BTN_WARN_ORANGE":   "#c47e00",
    "BTN_WARN_ORANGE_HOV":"#a86b00",

    # Buttons — blues
    "BTN_INFO":          "#3584e4",
    "BTN_INFO_HOV":      "#1c71d8",
    "BTN_INFO_DEEP":     "#1961bd",
    "BTN_INFO_DEEP_HOV": "#1552a0",
    "BTN_NEUTRAL":       "#4a565d",
    "BTN_NEUTRAL_HOV":   "#506069",

    # Buttons — greys
    "BTN_GREY":        "#d5d4d0",
    "BTN_GREY_HOV":    "#d9d8d4",
    "BTN_GREY_ALT":    "#c0bfbc",
    "BTN_GREY_ALT_HOV":"#c7c6c4",

    # Buttons — purples
    "BTN_PURPLE":     "#9141ac",
    "BTN_PURPLE_HOV": "#813d9c",

    # Tree tags
    "TAG_FOLDER":       "#3584e4",
    "TAG_BSA":          "#e5a50a",
    "TAG_BSA_ALT":      "#5cb8d6",
    "TAG_INI_PROFILE":  "#26a269",
    "TAG_BUNDLED_FG":   "#3584e4",
    "TAG_BUNDLED_BG":   "#eef4fa",
    "TAG_INSTALLED_BG": "#def5e5",
    "TAG_UNORDERED_FG": "#77767b",

    # Tones
    "TONE_GREEN":     "#26a269",
    "TONE_RED":       "#e01b24",
    "TONE_BLUE":      "#3584e4",
    "TONE_CYAN":      "#26a269",
    "TONE_BLUE_SOFT": "#5e99eb",
    "TONE_FLAG":      "#e5a50a",

    # Scrollbars
    "SCROLL_BG":     "#deddda",
    "SCROLL_TROUGH": "#f6f5f4",
    "SCROLL_ACTIVE": "#3584e4",

    # Overlays / special
    "BG_OVERLAY_ERR":  "#fcebeb",
    "BG_OVERLAY_DEEP": "#f6f5f4",
    "BG_CARD":         "#ffffff",
    "BG_CARD_ALT":     "#fafafa",
    "BG_GREEN_ROW":    "#def5e5",
    "BG_GREEN_DEEP":   "#cff2de",
    "BG_RED_DEEP":     "#fcebeb",
    "BG_ORANGE_DEEP":  "#fdf5e6",
    "BG_GREEN_TEXT":   "#1a7d3a",
    "BG_RED_TEXT":     "#a51d2d",
    "BG_ORANGE_TEXT":  "#856404",
    "BG_BLUE_DEEP":    "#eef4fa",
    "BG_BLUE_TEXT":    "#1961bd",
    "BG_DARK_BLUE":    "#eef4fa",
    "BG_DARK_GREEN":   "#def5e5",
    "BG_ENTRY":        "#e6e6e7",
    "BG_BTN_SAVE":     "#3584e4",
    "BG_SELECT_BAR":   "#eef4fa",
    "BG_MOD_REQ":      "#26a269",
    "BG_MOD_OPT":      "#e5a50a",

    # Status
    "STATUS_ERR_BRIGHT":    "#e01b24",
    "STATUS_BADGE_RED":     "#e01b24",
    "STATUS_BADGE_GREEN":   "#26a269",
    "STATUS_SUCCESS_SOLID": "#26a269",
    "STATUS_QUEUED":        "#e5a50a",
    "STATUS_DL_GREEN":      "#26a269",

    # Card text
    "TEXT_CARD":     "#262626",
    "TEXT_CARD_DIM": "#5e5c64",
    "TEXT_CARD_MED": "#3d3846",
    "TEXT_TREE_FG":  "#26a269",

    # CTk light/dark tuples
    "CTK_TEXT":       ["#262626", "#ffffff"],
    "CTK_FOOTER_FG":  ["#5e5c64", "#ffffff"],
    "CTK_FOOTER_HOV": ["#3d3846", "#f6f5f4"],
    "CTK_SEP":        ["#c0bfbc", "#2b2b2b"],
    "CTK_SEP_ALT":    ["#d5d4d0", "#333333"],
    "CTK_BTN_HOVER":  ["gray90", "gray25"],

    # Dropdown / combobox arrow glyph (tinted via QSS-generated PNG)
    "DROPDOWN_ARROW": "#3584e4",

    # Misc
    "LINK_BLUE":     "#3584e4",

    # Plugin-cycle status rows (Show Cycle view)
    "PLUGIN_CYCLE_ERR_BG":  "#fcebeb",
    "PLUGIN_CYCLE_ERR_FG":  "#a51d2d",
    "PLUGIN_CYCLE_OK_BG":   "#def5e5",
    "PLUGIN_CYCLE_OK_FG":   "#1a7d3a",
    "PLUGIN_CYCLE_WARN_BG": "#fdf5e6",
    "PLUGIN_CYCLE_WARN_FG": "#856404",
    "PLUGIN_CYCLE_ANCHOR":  "#9c590a",
    "PLUGIN_CYCLE_LINK":    "#3584e4",

    # File conflict states (Data / Mod Files / plugin conflicts)
    "FILE_WIN":      "#26a269",
    "FILE_LOSE":     "#e01b24",
    "FILE_DIM":      "#77767b",
    "FILE_ANCHOR":   "#9c590a",

    # Drag selection outline (modlist / plugins)
    "HIGHLIGHT_DRAG": "#3584e4",

    # Cross-panel conflict row highlights (modlist / plugins / data tree)
    "CONFLICT_HL_WIN":    "#26a269",   # selection beats this mod (green)
    "CONFLICT_HL_LOSE":   "#e01b24",   # this mod beats selection (red)
    "CONFLICT_HL_ANCHOR": "#9c590a",   # plugin-selected / anchor mod (orange)
    "REQ_HL_REQUIRES":    "#9141ac",   # mods the selection requires (purple)
    "REQ_HL_REQUIRED_BY": "#1961bd",   # mods that require the selection (blue)

    # Framework-status banner rows (Plugins tab) — per install state
    "FRAMEWORK_INSTALLED_BG": "#def5e5", "FRAMEWORK_INSTALLED_FG": "#1a7d3a",
    "FRAMEWORK_STAGED_BG":    "#fdf5e6", "FRAMEWORK_STAGED_FG":    "#856404",
    "FRAMEWORK_DISABLED_BG":  "#eef4fa", "FRAMEWORK_DISABLED_FG":  "#1961bd",
    "FRAMEWORK_MISSING_BG":   "#fcebeb", "FRAMEWORK_MISSING_FG":   "#a51d2d",

    # Modlist boundary separator bands (pinned Overwrite / Root Folder rows)
    "OVERWRITE_SEP_BG": "#def5e5", "OVERWRITE_SEP_FG": "#33d17a",
    "ROOT_SEP_BG":      "#99c1f1", "ROOT_SEP_FG":      "#eef4fa",

    # Checkbox fill when checked (tick auto-contrasts off this)
    "CHECK_FILL": "#3584e4",
}
