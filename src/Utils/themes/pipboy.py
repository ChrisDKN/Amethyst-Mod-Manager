"""Pip-Boy - a green-phosphor CRT theme.

Near-black surfaces with a single green phosphor ramp for everything
structural, plus a bright green selection bar that inverts to dark text the way
a Pip-Boy's menus do. Amber and red are the only other hues, and they are
reserved for warning and danger so those states stay findable.

Two deliberate exceptions to the monochrome rule: the "requires" and "required
by" mod highlights keep an off-green hue (teal and soft blue). Those two mark
different relationships on rows that may sit next to conflict-green and
anchor-amber ones, and separating four states by luminance alone stopped being
readable. Legibility wins over the CRT conceit there.

Declares the SCANLINE_* keys, so selecting this theme also switches on the
click-through scanline sheet from ``gui_qt/scanline_overlay.py``. Those four
keys are ignored by every other theme and by the theme editor.

Every key here must also exist in every other theme file. If you add a new
constant, add it to every theme or the app will break when that theme is
selected.
"""

NAME = "Pip-Boy"

CTK_APPEARANCE = "dark"

# Phosphor ramp used throughout:
#   #c8ffd9  wash      (the "white" of a green CRT)
#   #8bffb4  pale
#   #4dff88  bright
#   #1aff6a  phosphor  (accent / selection / text on black)
#   #12b04f  mid
#   #0f7434  dim
#   #144a22  deep      (borders, separators)
# Off-ramp hues: amber #ffb52e (warn/anchor), red #ff3b2f (danger).

# Theme-aware defaults for the legacy/Tk conflict and separator colour hooks.
THEME_DEFAULTS_OVERRIDE: dict[str, str] = {
    "conflict_higher":    "#1aff6a",
    "conflict_lower":     "#ff3b2f",
    "plugin_mod":         "#12b04f",
    "plugin_separator":   "#4dff88",
    "conflict_separator": "#144a22",
    "separator_bg":       "#0f3a1c",
}

PALETTE: dict[str, str | tuple] = {
    # Backgrounds - near-black with a green cast, stepped so panels read as
    # raised without ever getting light enough to wash out the phosphor.
    "BG_DEEP":       "#040a05",
    "BG_PANEL":      "#0a1a0d",
    "BG_HEADER":     "#0d2110",
    "BG_ROW":        "#071309",
    "BG_ROW_ALT":    "#0a190c",
    "BG_ROW_HOVER":  "#123d1e",
    "BG_LIST":       "#050f07",
    "BG_SEP":        "#124a22",
    "BG_HOVER":      "#16552a",
    "BG_SELECT":     "#1aff6a",   # inverted selection bar, as on the device
    "BG_HOVER_ROW":  "#123d1e",

    # Accents - phosphor green, with near-black text on top of it.
    "ACCENT":        "#1aff6a",
    "ACCENT_HOV":    "#5cff9a",
    "TEXT_ON_ACCENT":"#04160a",

    # Text - one green ramp; "white" is the pale phosphor wash.
    "TEXT_MAIN":     "#2bf06e",
    "TEXT_DIM":      "#17a349",
    "TEXT_MUTED":    "#1fc55c",
    "TEXT_FAINT":    "#0f7434",
    "TEXT_SEP":      "#8bffb4",
    "TEXT_WHITE":    "#c8ffd9",
    "TEXT_BLACK":    "#04160a",
    "TEXT_OK":       "#4dff88",
    "TEXT_ERR":      "#ff5a4d",
    "TEXT_WARN":     "#ffb52e",
    "TEXT_OK_BRIGHT":   "#8bffb4",
    "TEXT_ERR_BRIGHT":  "#ff7a6e",
    "TEXT_WARN_BRIGHT": "#ffc75c",

    # Borders - deep green so frames read as etched, not as grey chrome.
    "BORDER":        "#144a22",
    "BORDER_DIM":    "#1c6b31",
    "BORDER_FAINT":  "#25913f",

    # Buttons - reds (danger keeps a real red; the device uses it too)
    "RED_BTN":       "#c22b1e",
    "RED_HOV":       "#ff3b2f",
    "BTN_DANGER":        "#c22b1e",
    "BTN_DANGER_HOV":    "#ff3b2f",
    "BTN_DANGER_ALT":    "#8c1e14",
    "BTN_DANGER_ALT_HOV":"#b32a1d",
    "BTN_DANGER_DEEP":   "#661610",
    "BTN_DANGER_DEEP_HOV":"#8c1e14",
    "BTN_CANCEL":        "#c22b1e",
    "BTN_CANCEL_HOV":    "#8c1e14",

    # Buttons - greens
    "BTN_SUCCESS":          "#1aff6a",
    "BTN_SUCCESS_HOV":      "#5cff9a",
    "BTN_SUCCESS_ALT":      "#12b04f",
    "BTN_SUCCESS_ALT_HOV":  "#16c95c",
    "BTN_SUCCESS_DEEP":     "#0f7434",
    "BTN_SUCCESS_DEEP_HOV": "#12903f",

    # Buttons - ambers
    "BTN_WARN":          "#ffb52e",
    "BTN_WARN_HOV":      "#ffc75c",
    "BTN_WARN_DEEP":     "#b57a12",
    "BTN_WARN_DEEP_HOV": "#d1911e",
    "BTN_WARN_BROWN":    "#7a5a12",
    "BTN_WARN_BROWN_HOV":"#96721e",
    "BTN_WARN_ORANGE":   "#ffb52e",
    "BTN_WARN_ORANGE_HOV":"#ffc75c",

    # Buttons - "info" has no blue to spend, so it steps down the green ramp.
    "BTN_INFO":          "#12b04f",
    "BTN_INFO_HOV":      "#16c95c",
    "BTN_INFO_DEEP":     "#0f7434",
    "BTN_INFO_DEEP_HOV": "#12903f",
    "BTN_NEUTRAL":       "#123d1e",
    "BTN_NEUTRAL_HOV":   "#16552a",

    # Buttons - "greys" are the darkest green surfaces, not neutral greys.
    "BTN_GREY":        "#0d2110",
    "BTN_GREY_HOV":    "#144a22",
    "BTN_GREY_ALT":    "#0a1a0d",
    "BTN_GREY_ALT_HOV":"#123d1e",

    # Buttons - purples (mapped onto the ramp)
    "BTN_PURPLE":     "#12b04f",
    "BTN_PURPLE_HOV": "#16c95c",

    # Tree tags
    "TAG_FOLDER":       "#4dff88",
    "TAG_BSA":          "#ffb52e",
    "TAG_BSA_ALT":      "#8bffb4",
    "TAG_INI_PROFILE":  "#2ee6c0",
    "TAG_BUNDLED_FG":   "#8bffb4",
    "TAG_BUNDLED_BG":   "#0c2e16",
    "TAG_INSTALLED_BG": "#0f3a1c",
    "TAG_UNORDERED_FG": "#0f7434",

    # Tones
    "TONE_GREEN":     "#4dff88",
    "TONE_RED":       "#ff5a4d",
    "TONE_BLUE":      "#2ee6c0",
    "TONE_CYAN":      "#7dffae",
    "TONE_BLUE_SOFT": "#a8fff2",
    "TONE_FLAG":      "#ffb52e",

    # Scrollbars - deep green trough, phosphor thumb when active.
    "SCROLL_BG":     "#144a22",
    "SCROLL_TROUGH": "#050f07",
    "SCROLL_ACTIVE": "#1aff6a",

    # Overlays / special
    "BG_OVERLAY_ERR":  "#0f0605",
    "BG_OVERLAY_DEEP": "#020604",
    "BG_CARD":         "#0a1a0d",
    "BG_CARD_ALT":     "#071309",
    "BG_GREEN_ROW":    "#0f3a1c",
    "BG_GREEN_DEEP":   "#0c2e16",
    "BG_RED_DEEP":     "#3a1210",
    "BG_ORANGE_DEEP":  "#3a2c0c",
    "BG_GREEN_TEXT":   "#c8ffd9",
    "BG_RED_TEXT":     "#ffc9c2",
    "BG_ORANGE_TEXT":  "#ffe0a0",
    "BG_BLUE_DEEP":    "#0c2e2a",
    "BG_BLUE_TEXT":    "#a8fff2",
    "BG_DARK_BLUE":    "#04120f",
    "BG_DARK_GREEN":   "#04120a",
    "BG_ENTRY":        "#020703",
    "BG_BTN_SAVE":     "#0f7434",
    "BG_SELECT_BAR":   "#0f3a1c",
    "BG_MOD_REQ":      "#0f8c3c",
    "BG_MOD_OPT":      "#b57a12",

    # Status
    "STATUS_ERR_BRIGHT":    "#ff5a4d",
    "STATUS_BADGE_RED":     "#c22b1e",
    "STATUS_BADGE_GREEN":   "#12b04f",
    "STATUS_SUCCESS_SOLID": "#1aff6a",
    "STATUS_QUEUED":        "#ffb52e",
    "STATUS_DL_GREEN":      "#1aff6a",

    # Card text
    "TEXT_CARD":     "#2bf06e",
    "TEXT_CARD_DIM": "#0f7434",
    "TEXT_CARD_MED": "#8bffb4",
    "TEXT_TREE_FG":  "#4dff88",

    # CTk light/dark tuples - keep tuples so built-in CTk widgets still adapt.
    "CTK_TEXT":       ("#000000", "#c8ffd9"),
    "CTK_FOOTER_FG":  ("#EBECF0", "#0a1a0d"),
    "CTK_FOOTER_HOV": ("#DFE1E5", "#123d1e"),
    "CTK_SEP":        ("#C9CCD6", "#144a22"),
    "CTK_SEP_ALT":    ("#D0D0D0", "#0d2110"),
    "CTK_BTN_HOVER":  ("gray90", "gray15"),

    # Dropdown / combobox arrow glyph (tinted via QSS-generated PNG)
    "DROPDOWN_ARROW": "#1aff6a",

    # Misc - links are phosphor, not blue.
    "LINK_BLUE":     "#4dff88",

    # Plugin-cycle status rows (Show Cycle view)
    "PLUGIN_CYCLE_ERR_BG":  "#4a1512",
    "PLUGIN_CYCLE_ERR_FG":  "#ffc9c2",
    "PLUGIN_CYCLE_OK_BG":   "#0f3a1c",
    "PLUGIN_CYCLE_OK_FG":   "#c8ffd9",
    "PLUGIN_CYCLE_WARN_BG": "#4a3a12",
    "PLUGIN_CYCLE_WARN_FG": "#ffe0a0",
    "PLUGIN_CYCLE_ANCHOR":  "#ffb52e",
    "PLUGIN_CYCLE_LINK":    "#4dff88",

    # File conflict states (Data / Mod Files / plugin conflicts)
    "FILE_WIN":      "#1aff6a",
    "FILE_LOSE":     "#ff3b2f",
    "FILE_DIM":      "#0f7434",
    "FILE_ANCHOR":   "#ffb52e",

    # Drag selection outline (modlist / plugins)
    "HIGHLIGHT_DRAG": "#8bffb4",

    # Cross-panel conflict row highlights (modlist / plugins / data tree).
    # These fill a whole row behind its normal text, so they stay mid-dark -
    # the bright end of the ramp belongs to the selection bar alone.
    "CONFLICT_HL_WIN":    "#0f7a38",   # selection beats this mod
    "CONFLICT_HL_LOSE":   "#8c1e14",   # this mod beats selection
    "CONFLICT_HL_ANCHOR": "#8c6410",   # plugin-selected / anchor mod
    "REQ_HL_REQUIRES":    "#146b60",   # off-ramp on purpose - see module docs
    "REQ_HL_REQUIRED_BY": "#1d4a8b",

    # Framework-status banner rows (Plugins tab) - per install state
    "FRAMEWORK_INSTALLED_BG": "#1aff6a", "FRAMEWORK_INSTALLED_FG": "#04160a",
    "FRAMEWORK_STAGED_BG":    "#ffb52e", "FRAMEWORK_STAGED_FG":    "#2a1c02",
    "FRAMEWORK_DISABLED_BG":  "#0f7434", "FRAMEWORK_DISABLED_FG":  "#c8ffd9",
    "FRAMEWORK_MISSING_BG":   "#c22b1e", "FRAMEWORK_MISSING_FG":   "#ffe4e0",

    # Modlist boundary separator bands (pinned Overwrite / Root Folder rows).
    # Separated by luminance rather than hue, the way Noir does it, so the two
    # bands stay tellable apart without spending a second colour on it.
    "OVERWRITE_SEP_BG": "#0f3a1c", "OVERWRITE_SEP_FG": "#8bffb4",
    "ROOT_SEP_BG":      "#0a2612", "ROOT_SEP_FG":      "#4dff88",

    # Checkbox fill when checked (tick auto-contrasts off this)
    "CHECK_FILL": "#1aff6a",

    # CRT scanline sheet - read by gui_qt/scanline_overlay.py, ignored by every
    # other theme. The lit row does most of the work: darkening a near-black
    # panel has no headroom, so the dark line alone only shows up on OLED.
    # Tune with AMM_SCANLINES=<n> before changing these.
    "SCANLINE_COLOR":      "#000000",
    "SCANLINE_ALPHA":      "96",
    "SCANLINE_PITCH":      "3",
    "SCANLINE_THICKNESS":  "1",
    "SCANLINE_GLOW_COLOR": "#8bffb4",   # phosphor bloom, not a white wash
    "SCANLINE_GLOW_ALPHA": "22",
}
