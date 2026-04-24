"""
Light theme.

Every key here must also exist in every other theme file. If you add a new
constant, add it to every theme or the app will break when that theme is
selected.
"""

NAME = "Light"

# Which CTk appearance mode this theme should use for built-in CustomTkinter
# widgets that read from ctk.ThemeManager (CTkLoader, default frame fills,
# etc.). Must be "light" or "dark" — CTk doesn't understand custom names.
CTK_APPEARANCE = "light"

# Per-theme defaults for the user-customisable keys (Settings → Theme).
# Only listed keys differ from the app-wide THEME_DEFAULTS. User picks in
# amethyst.ini always win; these only apply when the key is unset or still
# matches the dark default (treated as legacy).
THEME_DEFAULTS_OVERRIDE: dict[str, str] = {
    "separator_bg":       "#b8b8b8",
    "conflict_separator": "#a8a8a8",
}

PALETTE: dict[str, str | tuple] = {
    # Backgrounds — near-white bases with enough separation that gutter,
    # alternating panels, and alternating rows are each readable.
    "BG_DEEP":       "#9c9c9c",   # app gutter / outer scroll bg
    "BG_PANEL":      "#e0e0e0",   # panel A / primary surface (grey so white inputs on it contrast)
    "BG_HEADER":     "#b4b4b4",   # panel B / column headers / button-on-panel
    "BG_ROW":        "#ffffff",   # list row / textbox fill
    "BG_ROW_ALT":    "#d8d8d8",   # alternating row (~40-unit delta for non-OLED panels)
    "BG_ROW_HOVER":  "#b8b8b8",
    "BG_LIST":       "#dbdbdb",   # tree/list surface (matches CTk gray86 for CTkTreeview parity)
    "BG_SEP":        "#b8b8b8",
    "BG_HOVER":      "#b8d4f0",
    "BG_SELECT":     "#8cbde8",
    "BG_HOVER_ROW":  "#b8b8b8",

    # Accents — keep the same blue (works in both modes)
    "ACCENT":        "#0078d4",
    "ACCENT_HOV":    "#1084d8",
    "TEXT_ON_ACCENT":"#ffffff",   # text colour that reads on ACCENT/ACCENT_HOV

    # Text — dark on light
    "TEXT_MAIN":     "#1e1e1e",
    "TEXT_DIM":      "#555555",
    "TEXT_MUTED":    "#6b6b6b",
    "TEXT_FAINT":    "#8a8a8a",
    "TEXT_SEP":      "#404040",
    "TEXT_WHITE":    "#ffffff",
    "TEXT_BLACK":    "#000000",
    "TEXT_OK":       "#2e7a2e",
    "TEXT_ERR":      "#b02020",
    "TEXT_WARN":     "#a06a00",
    "TEXT_OK_BRIGHT":   "#2e7a2e",
    "TEXT_ERR_BRIGHT":  "#b02020",
    "TEXT_WARN_BRIGHT": "#a06a00",

    # Borders — visible greys (need enough contrast against BG_PANEL
    # for 1px divider lines to read on non-OLED monitors).
    "BORDER":        "#8a8a8a",
    "BORDER_DIM":    "#9a9a9a",
    "BORDER_FAINT":  "#a8a8a8",

    # Buttons — reds (slightly darker so text reads on lighter bg)
    "RED_BTN":       "#c43c3c",
    "RED_HOV":       "#d44848",
    "BTN_DANGER":        "#c94848",
    "BTN_DANGER_HOV":    "#b33a3a",
    "BTN_DANGER_ALT":    "#a83232",
    "BTN_DANGER_ALT_HOV":"#8b1a1a",
    "BTN_DANGER_DEEP":   "#8b1a1a",
    "BTN_DANGER_DEEP_HOV":"#7a1a1a",
    "BTN_CANCEL":        "#c0392b",
    "BTN_CANCEL_HOV":    "#a93226",

    # Buttons — greens
    "BTN_SUCCESS":          "#3a9e3a",
    "BTN_SUCCESS_HOV":      "#2d7a2d",
    "BTN_SUCCESS_ALT":      "#3a8a3d",
    "BTN_SUCCESS_ALT_HOV":  "#2e6b30",
    "BTN_SUCCESS_DEEP":     "#369150",
    "BTN_SUCCESS_DEEP_HOV": "#2a6e3f",

    # Buttons — oranges
    "BTN_WARN":          "#e28b00",
    "BTN_WARN_HOV":      "#c37800",
    "BTN_WARN_DEEP":     "#a07800",
    "BTN_WARN_DEEP_HOV": "#7a5a00",
    "BTN_WARN_BROWN":    "#7a5200",
    "BTN_WARN_BROWN_HOV":"#5a3a00",
    "BTN_WARN_ORANGE":   "#d97000",
    "BTN_WARN_ORANGE_HOV":"#b35a00",

    # Buttons — blues
    "BTN_INFO":          "#2a6aab",
    "BTN_INFO_HOV":      "#1e4d7a",
    "BTN_INFO_DEEP":     "#2070a8",
    "BTN_INFO_DEEP_HOV": "#1a5a8a",
    "BTN_NEUTRAL":       "#4a70aa",
    "BTN_NEUTRAL_HOV":   "#3a5a8a",

    # Buttons — greys
    "BTN_GREY":        "#c8c8c8",
    "BTN_GREY_HOV":    "#b8b8b8",
    "BTN_GREY_ALT":    "#d4d4d4",
    "BTN_GREY_ALT_HOV":"#c0c0c0",

    # Buttons — purples
    "BTN_PURPLE":     "#9b3fd0",
    "BTN_PURPLE_HOV": "#7b2fa8",

    # Tree tags — darker saturations that read on light bg
    "TAG_FOLDER":       "#1e7a8a",
    "TAG_BSA":          "#8a6a00",
    "TAG_BSA_ALT":      "#1e7a8a",
    "TAG_INI_PROFILE":  "#006a80",
    "TAG_BUNDLED_FG":   "#1a5c8a",
    "TAG_BUNDLED_BG":   "#d8e4f0",
    "TAG_INSTALLED_BG": "#d0e8d0",
    "TAG_UNORDERED_FG": "#888888",

    # Tones
    "TONE_GREEN":     "#2e7a2e",
    "TONE_RED":       "#b02020",
    "TONE_BLUE":      "#1e5a8a",
    "TONE_CYAN":      "#1e7a8a",
    "TONE_BLUE_SOFT": "#3a5a9a",
    "TONE_FLAG":      "#a06a00",

    # Scrollbars
    "SCROLL_BG":     "#c8c8c8",
    "SCROLL_TROUGH": "#e8e8e8",
    "SCROLL_ACTIVE": "#0078d4",

    # Overlays / special
    "BG_OVERLAY_ERR":  "#f8e0e0",
    "BG_OVERLAY_DEEP": "#e8e8e8",
    "BG_CARD":         "#ffffff",
    "BG_CARD_ALT":     "#f5f5f5",
    "BG_GREEN_ROW":    "#d0e8d0",
    "BG_GREEN_DEEP":   "#c8e0c8",
    "BG_RED_DEEP":     "#f0d0d0",
    "BG_GREEN_TEXT":   "#1a4d1a",
    "BG_RED_TEXT":     "#6b1a1a",
    "BG_DARK_BLUE":    "#b4c8e4",   # Root Folder separator — saturated so it reads on grey panel
    "BG_DARK_GREEN":   "#b4d8b4",   # Overwrite separator — same treatment
    "BG_ENTRY":        "#ffffff",
    "BG_BTN_SAVE":     "#5a5a9a",
    "BG_SELECT_BAR":   "#c0c8e0",
    "BG_MOD_REQ":      "#3a9e3a",
    "BG_MOD_OPT":      "#e28b00",

    # Status
    "STATUS_ERR_BRIGHT":    "#c42020",
    "STATUS_BADGE_RED":     "#c0392b",
    "STATUS_BADGE_GREEN":   "#2a8c2a",
    "STATUS_SUCCESS_SOLID": "#2ea74d",
    "STATUS_QUEUED":        "#c37800",
    "STATUS_DL_GREEN":      "#2e8e40",

    # Card text
    "TEXT_CARD":     "#2a2a2a",
    "TEXT_CARD_DIM": "#6a6a6a",
    "TEXT_CARD_MED": "#404040",
    "TEXT_TREE_FG":  "#2e7a2e",

    # CTk light/dark tuples — tuples stay identical across themes.
    "CTK_TEXT":       ("#000000", "#FFFFFF"),
    "CTK_FOOTER_FG":  ("#EBECF0", "#393B40"),
    "CTK_FOOTER_HOV": ("#DFE1E5", "#43454A"),
    "CTK_SEP":        ("#C9CCD6", "#5A5D63"),
    "CTK_SEP_ALT":    ("#D0D0D0", "#505050"),
    "CTK_BTN_HOVER":  ("gray90", "gray25"),

    # Misc
    "LINK_BLUE":     "#0a5ad4",
}
