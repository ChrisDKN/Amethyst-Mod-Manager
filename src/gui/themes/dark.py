"""
Dark theme — the app's original palette.

Every key here must also exist in every other theme file. If you add a new
constant, add it to every theme or the app will break when that theme is
selected.
"""

NAME = "Dark"

# Which CTk appearance mode this theme should use for built-in CustomTkinter
# widgets that read from ctk.ThemeManager (CTkLoader, default frame fills,
# etc.). Must be "light" or "dark" — CTk doesn't understand custom names.
CTK_APPEARANCE = "dark"

PALETTE: dict[str, str | tuple] = {
    # Backgrounds
    "BG_DEEP":       "#1a1a1a",
    "BG_PANEL":      "#252526",
    "BG_HEADER":     "#2a2a2b",
    "BG_ROW":        "#2d2d2d",
    "BG_ROW_ALT":    "#303030",
    "BG_ROW_HOVER":  "#3d3d3d",
    "BG_LIST":       "#212121",   # tree/list surface (matches CTk gray13 for CTkTreeview parity)
    "BG_SEP":        "#383838",   # overridden via load_theme_colors
    "BG_HOVER":      "#094771",
    "BG_SELECT":     "#0f5fa3",
    "BG_HOVER_ROW":  "#3d3d3d",

    # Accents
    "ACCENT":        "#0078d4",
    "ACCENT_HOV":    "#1084d8",
    "TEXT_ON_ACCENT":"#ffffff",   # text colour that reads on ACCENT/ACCENT_HOV

    # Text
    "TEXT_MAIN":     "#d4d4d4",
    "TEXT_DIM":      "#858585",
    "TEXT_MUTED":    "#aaaaaa",
    "TEXT_FAINT":    "#888888",
    "TEXT_SEP":      "#b0b0b0",
    "TEXT_WHITE":    "#ffffff",
    "TEXT_BLACK":    "#000000",
    "TEXT_OK":       "#98c379",
    "TEXT_ERR":      "#e06c75",
    "TEXT_WARN":     "#e5c07b",
    "TEXT_OK_BRIGHT":   "#6bc76b",
    "TEXT_ERR_BRIGHT":  "#e06c6c",
    "TEXT_WARN_BRIGHT": "#e5a04a",

    # Borders
    "BORDER":        "#444444",
    "BORDER_DIM":    "#555555",
    "BORDER_FAINT":  "#666666",

    # Buttons — reds
    "RED_BTN":       "#a83232",
    "RED_HOV":       "#c43c3c",
    "BTN_DANGER":        "#b33a3a",
    "BTN_DANGER_HOV":    "#c94848",
    "BTN_DANGER_ALT":    "#8b1a1a",
    "BTN_DANGER_ALT_HOV":"#b22222",
    "BTN_DANGER_DEEP":   "#7a1a1a",
    "BTN_DANGER_DEEP_HOV":"#a02020",
    "BTN_CANCEL":        "#c0392b",
    "BTN_CANCEL_HOV":    "#a93226",

    # Buttons — greens
    "BTN_SUCCESS":          "#2d7a2d",
    "BTN_SUCCESS_HOV":      "#3a9e3a",
    "BTN_SUCCESS_ALT":      "#2e6b30",
    "BTN_SUCCESS_ALT_HOV":  "#3a8a3d",
    "BTN_SUCCESS_DEEP":     "#2a6e3f",
    "BTN_SUCCESS_DEEP_HOV": "#369150",

    # Buttons — oranges
    "BTN_WARN":          "#c37800",
    "BTN_WARN_HOV":      "#e28b00",
    "BTN_WARN_DEEP":     "#7a5a00",
    "BTN_WARN_DEEP_HOV": "#a07800",
    "BTN_WARN_BROWN":    "#5a3a00",
    "BTN_WARN_BROWN_HOV":"#7a5200",
    "BTN_WARN_ORANGE":   "#b35a00",
    "BTN_WARN_ORANGE_HOV":"#d97000",

    # Buttons — blues
    "BTN_INFO":          "#1e4d7a",
    "BTN_INFO_HOV":      "#2a6aab",
    "BTN_INFO_DEEP":     "#1a5a8a",
    "BTN_INFO_DEEP_HOV": "#2070a8",
    "BTN_NEUTRAL":       "#3a5a8a",
    "BTN_NEUTRAL_HOV":   "#4a70aa",

    # Buttons — greys
    "BTN_GREY":        "#444444",
    "BTN_GREY_HOV":    "#555555",
    "BTN_GREY_ALT":    "#3c3c3c",
    "BTN_GREY_ALT_HOV":"#505050",

    # Buttons — purples
    "BTN_PURPLE":     "#7b2fa8",
    "BTN_PURPLE_HOV": "#9b3fd0",

    # Tree tags
    "TAG_FOLDER":       "#56b6c2",
    "TAG_BSA":          "#d8a657",
    "TAG_BSA_ALT":      "#56d8e4",
    "TAG_INI_PROFILE":  "#00e5ff",
    "TAG_BUNDLED_FG":   "#7ab8e8",
    "TAG_BUNDLED_BG":   "#1a2a3a",
    "TAG_INSTALLED_BG": "#1e4d1e",
    "TAG_UNORDERED_FG": "#888888",

    # Tones
    "TONE_GREEN":     "#98c379",
    "TONE_RED":       "#e06c75",
    "TONE_BLUE":      "#61afef",
    "TONE_CYAN":      "#7ec8e3",
    "TONE_BLUE_SOFT": "#7aa2f7",
    "TONE_FLAG":      "#e5c07b",

    # Scrollbars
    "SCROLL_BG":     "#383838",
    "SCROLL_TROUGH": "#1a1a1a",
    "SCROLL_ACTIVE": "#0078d4",

    # Overlays / special
    "BG_OVERLAY_ERR":  "#1a1a2e",
    "BG_OVERLAY_DEEP": "#1a1a1a",
    "BG_CARD":         "#333333",
    "BG_CARD_ALT":     "#2b2b2b",
    "BG_GREEN_ROW":    "#1a5c1a",
    "BG_GREEN_DEEP":   "#1b4d1b",
    "BG_RED_DEEP":     "#4d1b1b",
    "BG_GREEN_TEXT":   "#c8ffc8",
    "BG_RED_TEXT":     "#ffc8c8",
    "BG_DARK_BLUE":    "#1e1e2e",
    "BG_DARK_GREEN":   "#1e2a1e",
    "BG_ENTRY":        "#1e1e1e",
    "BG_BTN_SAVE":     "#4a4a8a",
    "BG_SELECT_BAR":   "#3a3a5a",
    "BG_MOD_REQ":      "#2d7a2d",
    "BG_MOD_OPT":      "#c37800",

    # Status
    "STATUS_ERR_BRIGHT":    "#ff6b6b",
    "STATUS_BADGE_RED":     "#e74c3c",
    "STATUS_BADGE_GREEN":   "#2a8c2a",
    "STATUS_SUCCESS_SOLID": "#00ff88",
    "STATUS_QUEUED":        "#ff9a3c",
    "STATUS_DL_GREEN":      "#4caf50",

    # Card text
    "TEXT_CARD":     "#cccccc",
    "TEXT_CARD_DIM": "#777777",
    "TEXT_CARD_MED": "#dddddd",
    "TEXT_TREE_FG":  "#6dbf6d",

    # CTk light/dark tuples — CustomTkinter picks one based on appearance mode.
    # These stay as tuples in every theme so built-in CTk widgets still adapt.
    "CTK_TEXT":       ("#000000", "#FFFFFF"),
    "CTK_FOOTER_FG":  ("#EBECF0", "#393B40"),
    "CTK_FOOTER_HOV": ("#DFE1E5", "#43454A"),
    "CTK_SEP":        ("#C9CCD6", "#5A5D63"),
    "CTK_SEP_ALT":    ("#D0D0D0", "#505050"),
    "CTK_BTN_HOVER":  ("gray90", "gray25"),

    # Misc
    "LINK_BLUE":     "#3574F0",
}
