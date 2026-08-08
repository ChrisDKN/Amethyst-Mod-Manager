"""Grouping + derivation metadata for the theme editor.

Pure data + colour maths, no Qt widgets. Two jobs:

1. ``GROUPS`` lays the ~180 palette keys out into human-labelled sections so the
   editor can render them in a sensible order instead of one flat wall of keys.

2. The derivation map lets a user edit a single *base* colour and have its
   related variants (hover / deep / alt) recomputed automatically. Each variant
   is produced by blending the base toward white (lighten) or black (darken) by
   a fixed factor. The factors were calibrated from the built-in ``dark`` palette
   so that, e.g., ``derive("BTN_CANCEL", <dark BTN_CANCEL>)`` reproduces the
   shipped ``BTN_CANCEL_HOV`` closely. The editor's "Advanced" mode bypasses
   derivation and exposes every key for direct editing.
"""

from __future__ import annotations


# --------------------------------------------------------------------------- #
# Colour maths
# --------------------------------------------------------------------------- #
def _rgb(hex_color: str) -> tuple[int, int, int] | None:
    h = hex_color.lstrip("#")
    if len(h) != 6:
        return None
    try:
        return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))  # type: ignore[return-value]
    except ValueError:
        return None


def blend(hex_color: str, factor: float) -> str:
    """Blend *hex_color* toward white (factor > 0) or black (factor < 0).

    factor is in -1..1; magnitude is the fraction of the way to the target.
    Returns the input unchanged if it isn't a #rrggbb string.
    """
    rgb = _rgb(hex_color)
    if rgb is None:
        return hex_color
    if factor >= 0:
        target = (255, 255, 255)
        f = min(1.0, factor)
    else:
        target = (0, 0, 0)
        f = min(1.0, -factor)
    r, g, b = (int(c + (t - c) * f) for c, t in zip(rgb, target))
    return f"#{r:02x}{g:02x}{b:02x}"


# --------------------------------------------------------------------------- #
# Derivation map: base key -> {variant key: blend factor}
#
# Positive factor = lighter (hover), negative = darker (deep). Factors chosen to
# approximate the dark palette's shipped base/variant pairs.
# --------------------------------------------------------------------------- #
DERIVE: dict[str, dict[str, float]] = {
    # Reds
    "RED_BTN":       {"RED_HOV": 0.12},
    "BTN_DANGER":    {"BTN_DANGER_HOV": 0.12},
    "BTN_DANGER_ALT":{"BTN_DANGER_ALT_HOV": 0.20},
    "BTN_DANGER_DEEP":{"BTN_DANGER_DEEP_HOV": 0.16},
    "BTN_CANCEL":    {"BTN_CANCEL_HOV": -0.10},   # cancel hover is slightly darker
    # Greens
    "BTN_SUCCESS":     {"BTN_SUCCESS_HOV": 0.16},
    "BTN_SUCCESS_ALT": {"BTN_SUCCESS_ALT_HOV": 0.14},
    "BTN_SUCCESS_DEEP":{"BTN_SUCCESS_DEEP_HOV": 0.14},
    # Oranges
    "BTN_WARN":       {"BTN_WARN_HOV": 0.14},
    "BTN_WARN_DEEP":  {"BTN_WARN_DEEP_HOV": 0.14},
    "BTN_WARN_BROWN": {"BTN_WARN_BROWN_HOV": 0.12},
    "BTN_WARN_ORANGE":{"BTN_WARN_ORANGE_HOV": 0.14},
    # Blues
    "BTN_INFO":      {"BTN_INFO_HOV": 0.22},
    "BTN_INFO_DEEP": {"BTN_INFO_DEEP_HOV": 0.14},
    "BTN_NEUTRAL":   {"BTN_NEUTRAL_HOV": 0.14},
    # Greys
    "BTN_GREY":     {"BTN_GREY_HOV": 0.10},
    "BTN_GREY_ALT": {"BTN_GREY_ALT_HOV": 0.12},
    # Purple
    "BTN_PURPLE":   {"BTN_PURPLE_HOV": 0.16},
    # Accent
    "ACCENT":       {"ACCENT_HOV": 0.06},
    # Rows
    "BG_ROW":       {"BG_ROW_ALT": 0.04, "BG_ROW_HOVER": 0.10, "BG_HOVER_ROW": 0.10},
}

# Set of keys that are variants driven by some base (hidden unless Advanced).
DERIVED_KEYS: set[str] = {v for variants in DERIVE.values() for v in variants}


def derive(base_key: str, hex_color: str) -> dict[str, str]:
    """Return ``{base_key: hex, <variant>: <computed>, ...}`` for a base edit.

    If *base_key* drives no variants, returns just ``{base_key: hex_color}``.
    """
    out = {base_key: hex_color}
    for variant, factor in DERIVE.get(base_key, {}).items():
        out[variant] = blend(hex_color, factor)
    return out


# --------------------------------------------------------------------------- #
# Group layout: [(section title, [(key, label), ...]), ...]
# Keys not listed here still appear under "Other" (built at runtime from the
# live palette) so a new palette key is never silently uneditable.
# --------------------------------------------------------------------------- #
GROUPS: list[tuple[str, list[tuple[str, str]]]] = [
    ("Backgrounds", [
        ("BG_DEEP", "App background (deepest)"),
        ("BG_PANEL", "Panel / card surface"),
        ("BG_HEADER", "Header / toolbar"),
        ("BG_ROW", "List row"),
        ("BG_ROW_ALT", "List row (alt stripe)"),
        ("BG_ROW_HOVER", "List row hover"),
        ("BG_LIST", "Tree / list surface"),
        ("BG_SEP", "Separator fill"),
        ("BG_HOVER", "Hover highlight"),
        ("BG_SELECT", "Selection highlight"),
        ("BG_ENTRY", "Text input field"),
    ]),
    ("Text", [
        ("TEXT_MAIN", "Primary text"),
        ("TEXT_DIM", "Dimmed text"),
        ("TEXT_MUTED", "Muted text"),
        ("TEXT_FAINT", "Faint text"),
        ("TEXT_SEP", "Separator text"),
        ("TEXT_WHITE", "White"),
        ("TEXT_BLACK", "Black"),
        ("TEXT_OK", "Success text"),
        ("TEXT_ERR", "Error text"),
        ("TEXT_WARN", "Warning text"),
        ("TEXT_OK_BRIGHT", "Success text (bright)"),
        ("TEXT_ERR_BRIGHT", "Error text (bright)"),
        ("TEXT_WARN_BRIGHT", "Warning text (bright)"),
        ("TEXT_CARD", "Card text"),
        ("TEXT_CARD_DIM", "Card text (dim)"),
        ("TEXT_CARD_MED", "Card text (medium)"),
        ("TEXT_TREE_FG", "Tree foreground"),
    ]),
    ("Accent", [
        ("ACCENT", "Accent"),
        ("ACCENT_HOV", "Accent hover"),
        ("TEXT_ON_ACCENT", "Text on accent"),
        ("LINK_BLUE", "Hyperlink"),
        ("DROPDOWN_ARROW", "Dropdown arrow"),
    ]),
    ("Borders", [
        ("BORDER", "Border"),
        ("BORDER_DIM", "Border (dim)"),
        ("BORDER_FAINT", "Border (faint)"),
    ]),
    ("Buttons - Red", [
        ("BTN_DANGER", "Danger"),
        ("BTN_DANGER_HOV", "Danger hover"),
        ("BTN_DANGER_ALT", "Danger (alt)"),
        ("BTN_DANGER_ALT_HOV", "Danger alt hover"),
        ("BTN_DANGER_DEEP", "Danger (deep)"),
        ("BTN_DANGER_DEEP_HOV", "Danger deep hover"),
        ("BTN_CANCEL", "Cancel"),
        ("BTN_CANCEL_HOV", "Cancel hover"),
        ("RED_BTN", "Red (legacy)"),
        ("RED_HOV", "Red hover (legacy)"),
    ]),
    ("Buttons - Green", [
        ("BTN_SUCCESS", "Success"),
        ("BTN_SUCCESS_HOV", "Success hover"),
        ("BTN_SUCCESS_ALT", "Success (alt)"),
        ("BTN_SUCCESS_ALT_HOV", "Success alt hover"),
        ("BTN_SUCCESS_DEEP", "Success (deep)"),
        ("BTN_SUCCESS_DEEP_HOV", "Success deep hover"),
    ]),
    ("Buttons - Orange", [
        ("BTN_WARN", "Warning"),
        ("BTN_WARN_HOV", "Warning hover"),
        ("BTN_WARN_DEEP", "Warning (deep)"),
        ("BTN_WARN_DEEP_HOV", "Warning deep hover"),
        ("BTN_WARN_BROWN", "Warning (brown)"),
        ("BTN_WARN_BROWN_HOV", "Warning brown hover"),
        ("BTN_WARN_ORANGE", "Warning (orange)"),
        ("BTN_WARN_ORANGE_HOV", "Warning orange hover"),
    ]),
    ("Buttons - Blue", [
        ("BTN_INFO", "Info"),
        ("BTN_INFO_HOV", "Info hover"),
        ("BTN_INFO_DEEP", "Info (deep)"),
        ("BTN_INFO_DEEP_HOV", "Info deep hover"),
        ("BTN_NEUTRAL", "Neutral"),
        ("BTN_NEUTRAL_HOV", "Neutral hover"),
    ]),
    ("Buttons - Grey", [
        ("BTN_GREY", "Grey"),
        ("BTN_GREY_HOV", "Grey hover"),
        ("BTN_GREY_ALT", "Grey (alt)"),
        ("BTN_GREY_ALT_HOV", "Grey alt hover"),
    ]),
    ("Buttons - Purple", [
        ("BTN_PURPLE", "Purple"),
        ("BTN_PURPLE_HOV", "Purple hover"),
    ]),
    ("Tree tags", [
        ("TAG_FOLDER", "Folder"),
        ("TAG_BSA", "BSA archive"),
        ("TAG_BSA_ALT", "BSA archive (alt)"),
        ("TAG_INI_PROFILE", "INI profile"),
        ("TAG_BUNDLED_FG", "Bundled (text)"),
        ("TAG_BUNDLED_BG", "Bundled (background)"),
        ("TAG_INSTALLED_BG", "Installed (background)"),
        ("TAG_UNORDERED_FG", "Unordered (text)"),
    ]),
    ("Tones", [
        ("TONE_GREEN", "Green tone"),
        ("TONE_RED", "Red tone"),
        ("TONE_BLUE", "Blue tone"),
        ("TONE_CYAN", "Cyan tone"),
        ("TONE_BLUE_SOFT", "Soft blue tone"),
        ("TONE_FLAG", "Flag tone"),
    ]),
    ("Scrollbars", [
        ("SCROLL_BG", "Scrollbar background"),
        ("SCROLL_TROUGH", "Scrollbar trough"),
        ("SCROLL_ACTIVE", "Scrollbar thumb (active)"),
    ]),
    ("Overlays & tinted rows", [
        ("BG_OVERLAY_ERR", "Error overlay"),
        ("BG_OVERLAY_DEEP", "Deep overlay"),
        ("BG_CARD", "Card"),
        ("BG_CARD_ALT", "Card (alt)"),
        ("BG_GREEN_ROW", "Green row"),
        ("BG_GREEN_DEEP", "Green (deep)"),
        ("BG_RED_DEEP", "Red (deep)"),
        ("BG_ORANGE_DEEP", "Orange (deep)"),
        ("BG_BLUE_DEEP", "Blue (deep)"),
        ("BG_GREEN_TEXT", "Green tint text"),
        ("BG_RED_TEXT", "Red tint text"),
        ("BG_ORANGE_TEXT", "Orange tint text"),
        ("BG_BLUE_TEXT", "Blue tint text"),
        ("BG_DARK_BLUE", "Dark blue"),
        ("BG_DARK_GREEN", "Dark green"),
        ("BG_BTN_SAVE", "Save button"),
        ("BG_SELECT_BAR", "Selection bar"),
        ("BG_MOD_REQ", "Required mod"),
        ("BG_MOD_OPT", "Optional mod"),
    ]),
    ("Status", [
        ("STATUS_ERR_BRIGHT", "Error (bright)"),
        ("STATUS_BADGE_RED", "Badge red"),
        ("STATUS_BADGE_GREEN", "Badge green"),
        ("STATUS_SUCCESS_SOLID", "Success (solid)"),
        ("STATUS_QUEUED", "Queued"),
        ("STATUS_DL_GREEN", "Download green"),
    ]),
    ("Plugin cycle & files", [
        ("PLUGIN_CYCLE_ERR_BG", "Cycle error row (bg)"),
        ("PLUGIN_CYCLE_ERR_FG", "Cycle error row (text)"),
        ("PLUGIN_CYCLE_OK_BG", "Cycle ok row (bg)"),
        ("PLUGIN_CYCLE_OK_FG", "Cycle ok row (text)"),
        ("PLUGIN_CYCLE_WARN_BG", "Cycle warn row (bg)"),
        ("PLUGIN_CYCLE_WARN_FG", "Cycle warn row (text)"),
        ("PLUGIN_CYCLE_ANCHOR", "Cycle anchor"),
        ("PLUGIN_CYCLE_LINK", "Cycle link"),
        ("FILE_WIN", "File winning"),
        ("FILE_LOSE", "File overridden"),
        ("FILE_DIM", "File dim"),
        ("FILE_ANCHOR", "File anchor"),
        ("HIGHLIGHT_DRAG", "Drag selection outline"),
    ]),
    ("Conflict highlights", [
        ("CONFLICT_HL_WIN", "Conflict row - winning"),
        ("CONFLICT_HL_LOSE", "Conflict row - overridden"),
        ("CONFLICT_HL_ANCHOR", "Conflict row - anchor"),
        ("REQ_HL_REQUIRES", "Requirement row - requires"),
        ("REQ_HL_REQUIRED_BY", "Requirement row - required by"),
    ]),
    ("Framework detection", [
        ("FRAMEWORK_INSTALLED_BG", "Installed (bg)"),
        ("FRAMEWORK_INSTALLED_FG", "Installed (text)"),
        ("FRAMEWORK_STAGED_BG", "Staged (bg)"),
        ("FRAMEWORK_STAGED_FG", "Staged (text)"),
        ("FRAMEWORK_DISABLED_BG", "Disabled (bg)"),
        ("FRAMEWORK_DISABLED_FG", "Disabled (text)"),
        ("FRAMEWORK_MISSING_BG", "Missing (bg)"),
        ("FRAMEWORK_MISSING_FG", "Missing (text)"),
    ]),
    ("Separator bands", [
        ("OVERWRITE_SEP_BG", "Overwrite band (bg)"),
        ("OVERWRITE_SEP_FG", "Overwrite band (text)"),
        ("ROOT_SEP_BG", "Root Folder band (bg)"),
        ("ROOT_SEP_FG", "Root Folder band (text)"),
    ]),
    ("Checkboxes", [
        ("CHECK_FILL", "Checkbox fill (checked)"),
    ]),
]

# One-line "where does this show up" hint per section, rendered under the group
# title in the editor so it's obvious what each block of colours affects.
GROUP_DESCRIPTIONS: dict[str, str] = {
    "Backgrounds": "Window, panels, list rows and input fields - the app's surfaces.",
    "Text": "Label and list text throughout the app, plus success/warning/error text.",
    "Accent": "The highlight colour: links, dropdown arrows and accented controls.",
    "Borders": "Lines and frames around panels, lists and inputs.",
    "Buttons - Red": "Danger / cancel / remove buttons (delete, remove profile, ✕ close).",
    "Buttons - Green": "Success / confirm buttons (Install, Done, Play).",
    "Buttons - Orange": "Warning buttons (Reinstall, download / update actions).",
    "Buttons - Blue": "Info / neutral action buttons (Select, Groups, Plugin Rules).",
    "Buttons - Grey": "Secondary / neutral buttons (View, minor actions).",
    "Buttons - Purple": "Accent buttons like Ko-Fi.",
    "Tree tags": "Coloured labels in file trees (folders, BSA archives, bundled/installed).",
    "Tones": "Shared accent tones reused by flags, icons and small highlights.",
    "Scrollbars": "The scrollbar track and thumb.",
    "Overlays & tinted rows": "Popup/overlay backgrounds and coloured info rows (required/optional mods, cards).",
    "Status": "Small status pills and badges (queued, download progress, error/success).",
    "Plugin cycle & files": "Plugin-cycle rows and file-conflict colours in the Data / Mod Files views.",
    "Conflict highlights": "Row tints when a conflicting mod is selected (winning / overridden / anchor), plus the requirement tints used by the View Requirements tab.",
    "Framework detection": "The framework-status banner above the Plugins list (installed / staged / disabled / missing).",
    "Separator bands": "The pinned Overwrite and Root Folder bands at the top of the modlist.",
    "Checkboxes": "The fill colour of a ticked checkbox (the tick stays auto-contrasted).",
}

# Flattened set of every key that appears in an explicit group above.
_KNOWN_KEYS: set[str] = {k for _, keys in GROUPS for k, _ in keys}


def is_editable_value(value) -> bool:
    """True for plain ``#rrggbb`` string values (skip CTk (light,dark) tuples
    and any non-hex entries the editor can't render as a single swatch)."""
    return isinstance(value, str) and value.startswith("#") and len(value) in (7,)


def grouped_for_palette(palette: dict) -> list[tuple[str, list[tuple[str, str]]]]:
    """Return GROUPS filtered to keys present in *palette*, plus a trailing
    "Other" group for any editable palette key not covered above (so new keys
    are never silently uneditable)."""
    result: list[tuple[str, list[tuple[str, str]]]] = []
    for title, keys in GROUPS:
        present = [(k, label) for k, label in keys if k in palette]
        if present:
            result.append((title, present))
    extras = [(k, k) for k, v in palette.items()
              if k not in _KNOWN_KEYS and is_editable_value(v)]
    if extras:
        result.append(("Other", sorted(extras)))
    return result
