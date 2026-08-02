"""
Amethyst — the modern signature theme.

A cool near-black neutral base (zinc family) with a violet accent, chosen to
finally match the app's name. Aims for a contemporary "Linear / Vercel" feel:
low-contrast surfaces, faint borders, a single confident accent, and semantic
button colours drawn from the Tailwind palette for consistency.

Every key here must also exist in every other theme file. If you add a new
constant, add it to every theme or the app will break when that theme is
selected. (This file was produced by copying dark.py and retuning values only —
no keys added or removed.)
"""

NAME = "Amethyst"

# Dark-based custom theme; built-in CTk widgets use the dark appearance.
CTK_APPEARANCE = "dark"

# Signature colours
#   violet   #8b5cf6 / #a78bfa   (accent / selection / focus)
#   base     #0e0e11 … #26262d   (cool near-black zinc surfaces)

PALETTE: dict[str, str | tuple] = {
    # Backgrounds — cool near-black zinc surfaces, softly layered.
    "BG_DEEP":       "#0e0e11",   # window background
    "BG_PANEL":      "#16161a",   # raised panel
    "BG_HEADER":     "#1b1b20",   # toolbar / header / button base
    "BG_ROW":        "#17171b",
    "BG_ROW_ALT":    "#1c1c21",   # zebra alt
    "BG_ROW_HOVER":  "#26262d",
    "BG_LIST":       "#121215",   # tree/list surface
    "BG_SEP":        "#26262d",
    "BG_HOVER":      "#241b3d",   # violet-tinted hover
    "BG_SELECT":     "#6d28d9",   # violet selection (white text reads on it)
    "BG_HOVER_ROW":  "#26262d",

    # Accents — amethyst violet.
    "ACCENT":        "#8b5cf6",
    "ACCENT_HOV":    "#a78bfa",
    "TEXT_ON_ACCENT":"#ffffff",

    # Text — crisp zinc greys (not pure white, easier on the eyes).
    "TEXT_MAIN":     "#e4e4e7",
    "TEXT_DIM":      "#7c7c88",
    "TEXT_MUTED":    "#a1a1aa",
    "TEXT_FAINT":    "#6b6b76",
    "TEXT_SEP":      "#b4b4be",
    "TEXT_WHITE":    "#ffffff",
    "TEXT_BLACK":    "#000000",
    "TEXT_OK":       "#4ade80",
    "TEXT_ERR":      "#f87171",
    "TEXT_WARN":     "#fbbf24",
    "TEXT_OK_BRIGHT":   "#86efac",
    "TEXT_ERR_BRIGHT":  "#fca5a5",
    "TEXT_WARN_BRIGHT": "#fcd34d",

    # Borders — subtle, low-contrast separators (modern faint-line look).
    "BORDER":        "#2a2a31",
    "BORDER_DIM":    "#35353d",
    "BORDER_FAINT":  "#45454f",

    # Buttons — reds
    "RED_BTN":       "#dc2626",
    "RED_HOV":       "#ef4444",
    "BTN_DANGER":        "#dc2626",
    "BTN_DANGER_HOV":    "#ef4444",
    "BTN_DANGER_ALT":    "#991b1b",
    "BTN_DANGER_ALT_HOV":"#b91c1c",
    "BTN_DANGER_DEEP":   "#7f1d1d",
    "BTN_DANGER_DEEP_HOV":"#991b1b",
    "BTN_CANCEL":        "#b91c1c",
    "BTN_CANCEL_HOV":    "#991b1b",

    # Buttons — greens
    "BTN_SUCCESS":          "#16a34a",
    "BTN_SUCCESS_HOV":      "#22c55e",
    "BTN_SUCCESS_ALT":      "#15803d",
    "BTN_SUCCESS_ALT_HOV":  "#16a34a",
    "BTN_SUCCESS_DEEP":     "#166534",
    "BTN_SUCCESS_DEEP_HOV": "#15803d",

    # Buttons — oranges
    "BTN_WARN":          "#d97706",
    "BTN_WARN_HOV":      "#f59e0b",
    "BTN_WARN_DEEP":     "#92400e",
    "BTN_WARN_DEEP_HOV": "#b45309",
    "BTN_WARN_BROWN":    "#78350f",
    "BTN_WARN_BROWN_HOV":"#92400e",
    "BTN_WARN_ORANGE":   "#ea580c",
    "BTN_WARN_ORANGE_HOV":"#f97316",

    # Buttons — blues
    "BTN_INFO":          "#2563eb",
    "BTN_INFO_HOV":      "#3b82f6",
    "BTN_INFO_DEEP":     "#1d4ed8",
    "BTN_INFO_DEEP_HOV": "#2563eb",
    "BTN_NEUTRAL":       "#4f46e5",
    "BTN_NEUTRAL_HOV":   "#6366f1",

    # Buttons — greys
    "BTN_GREY":        "#2e2e35",
    "BTN_GREY_HOV":    "#3a3a42",
    "BTN_GREY_ALT":    "#27272d",
    "BTN_GREY_ALT_HOV":"#35353d",

    # Buttons — purples
    "BTN_PURPLE":     "#7c3aed",
    "BTN_PURPLE_HOV": "#8b5cf6",

    # Tree tags
    "TAG_FOLDER":       "#22d3ee",
    "TAG_BSA":          "#fbbf24",
    "TAG_BSA_ALT":      "#67e8f9",
    "TAG_INI_PROFILE":  "#22d3ee",
    "TAG_BUNDLED_FG":   "#93c5fd",
    "TAG_BUNDLED_BG":   "#172033",
    "TAG_INSTALLED_BG": "#14311c",
    "TAG_UNORDERED_FG": "#6b6b76",

    # Tones
    "TONE_GREEN":     "#4ade80",
    "TONE_RED":       "#f87171",
    "TONE_BLUE":      "#60a5fa",
    "TONE_CYAN":      "#22d3ee",
    "TONE_BLUE_SOFT": "#818cf8",
    "TONE_FLAG":      "#fbbf24",

    # Scrollbars
    "SCROLL_BG":     "#35353d",
    "SCROLL_TROUGH": "#0e0e11",
    "SCROLL_ACTIVE": "#8b5cf6",

    # Overlays / special
    "BG_OVERLAY_ERR":  "#1a1420",
    "BG_OVERLAY_DEEP": "#0e0e11",
    "BG_CARD":         "#1c1c22",
    "BG_CARD_ALT":     "#17171b",
    "BG_GREEN_ROW":    "#14401f",
    "BG_GREEN_DEEP":   "#14311c",
    "BG_RED_DEEP":     "#3a1519",
    "BG_ORANGE_DEEP":  "#422006",
    "BG_GREEN_TEXT":   "#bbf7d0",
    "BG_RED_TEXT":     "#fecaca",
    "BG_ORANGE_TEXT":  "#fed7aa",
    "BG_BLUE_DEEP":    "#172033",
    "BG_BLUE_TEXT":    "#bfdbfe",
    "BG_DARK_BLUE":    "#16161f",
    "BG_DARK_GREEN":   "#14211a",
    "BG_ENTRY":        "#121215",
    "BG_BTN_SAVE":     "#4f46e5",
    "BG_SELECT_BAR":   "#2a2540",
    "BG_MOD_REQ":      "#16a34a",
    "BG_MOD_OPT":      "#d97706",

    # Status
    "STATUS_ERR_BRIGHT":    "#f87171",
    "STATUS_BADGE_RED":     "#ef4444",
    "STATUS_BADGE_GREEN":   "#22c55e",
    "STATUS_SUCCESS_SOLID": "#4ade80",
    "STATUS_QUEUED":        "#fb923c",
    "STATUS_DL_GREEN":      "#22c55e",

    # Card text
    "TEXT_CARD":     "#d4d4d8",
    "TEXT_CARD_DIM": "#71717a",
    "TEXT_CARD_MED": "#e4e4e7",
    "TEXT_TREE_FG":  "#4ade80",

    # CTk light/dark tuples — CustomTkinter picks one based on appearance mode.
    "CTK_TEXT":       ("#000000", "#FFFFFF"),
    "CTK_FOOTER_FG":  ("#EBECF0", "#1b1b20"),
    "CTK_FOOTER_HOV": ("#DFE1E5", "#26262d"),
    "CTK_SEP":        ("#C9CCD6", "#2a2a31"),
    "CTK_SEP_ALT":    ("#D0D0D0", "#35353d"),
    "CTK_BTN_HOVER":  ("gray90", "gray20"),

    # Dropdown / combobox arrow glyph (tinted via QSS-generated PNG)
    "DROPDOWN_ARROW": "#a78bfa",

    # Misc
    "LINK_BLUE":     "#818cf8",

    # Plugin-cycle status rows (Show Cycle view)
    "PLUGIN_CYCLE_ERR_BG":  "#3a1519",
    "PLUGIN_CYCLE_ERR_FG":  "#fecaca",
    "PLUGIN_CYCLE_OK_BG":   "#14311c",
    "PLUGIN_CYCLE_OK_FG":   "#bbf7d0",
    "PLUGIN_CYCLE_WARN_BG": "#422006",
    "PLUGIN_CYCLE_WARN_FG": "#fde68a",
    "PLUGIN_CYCLE_ANCHOR":  "#fb923c",   # "before" keyword
    "PLUGIN_CYCLE_LINK":    "#818cf8",   # "after" keyword

    # File conflict states (Data / Mod Files / plugin conflicts)
    "FILE_WIN":      "#16a34a",   # winning override (green)
    "FILE_LOSE":     "#dc2626",   # overridden (red)
    "FILE_DIM":      "#6b6b76",   # excluded / dim
    "FILE_ANCHOR":   "#ea580c",   # selected anchor plugin

    # Drag selection outline (modlist / plugins)
    "HIGHLIGHT_DRAG": "#a78bfa",

    # Cross-panel conflict row highlights (modlist / plugins / data tree)
    "CONFLICT_HL_WIN":    "#16a34a",   # selection beats this mod (green)
    "CONFLICT_HL_LOSE":   "#dc2626",   # this mod beats selection (red)
    "CONFLICT_HL_ANCHOR": "#ea580c",   # plugin-selected / anchor mod (orange)

    # Framework-status banner rows (Plugins tab) — per install state
    "FRAMEWORK_INSTALLED_BG": "#14311c", "FRAMEWORK_INSTALLED_FG": "#bbf7d0",
    "FRAMEWORK_STAGED_BG":    "#422006", "FRAMEWORK_STAGED_FG":    "#fed7aa",
    "FRAMEWORK_DISABLED_BG":  "#172033", "FRAMEWORK_DISABLED_FG":  "#bfdbfe",
    "FRAMEWORK_MISSING_BG":   "#3a1519", "FRAMEWORK_MISSING_FG":   "#fecaca",

    # Modlist boundary separator bands (pinned Overwrite / Root Folder rows)
    "OVERWRITE_SEP_BG": "#14211a", "OVERWRITE_SEP_FG": "#4ade80",
    "ROOT_SEP_BG":      "#16161f", "ROOT_SEP_FG":      "#818cf8",

    # Checkbox fill when checked (tick auto-contrasts off this)
    "CHECK_FILL": "#8b5cf6",
}
