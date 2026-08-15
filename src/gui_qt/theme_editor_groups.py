"""Grouping + derivation metadata for the theme editor.

Pure data + colour maths, no Qt widgets. Two jobs:

1. ``SIMPLE_GROUPS`` exposes the small semantic palette most authors need, while
   ``GROUPS`` keeps implemented app-specific roles available for fine tuning.

2. The derivation map lets a user edit a single *base* colour and have its
   related variants recomputed automatically. The source palette determines
   whether a variant moves lighter or darker, so light themes retain their
   intentionally darker hover states. Fine-tuning mode bypasses derivation.
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
    # Borders
    "BORDER":       {"BORDER_DIM": 0.10, "BORDER_FAINT": 0.18},
    # Rows
    "BG_ROW":       {"BG_ROW_ALT": 0.04, "BG_ROW_HOVER": 0.10, "BG_HOVER_ROW": 0.10},
}


def _luminance(hex_color: str) -> float | None:
    rgb = _rgb(hex_color)
    if rgb is None:
        return None
    return 0.299 * rgb[0] + 0.587 * rgb[1] + 0.114 * rgb[2]


def contrast_color(hex_color: str) -> str:
    """Return readable black/white text for a solid background colour."""
    lum = _luminance(hex_color)
    return "#000000" if lum is not None and lum > 140 else "#ffffff"


def derive(base_key: str, hex_color: str,
           source_palette: dict | None = None) -> dict[str, str]:
    """Return ``{base_key: hex, <variant>: <computed>, ...}`` for a base edit.

    If *source_palette* contains the existing base/variant pair, its luminance
    direction wins over the dark-theme-calibrated default. This preserves the
    darker hover states used by light themes.
    """
    out = {base_key: hex_color}
    for variant, factor in DERIVE.get(base_key, {}).items():
        if source_palette:
            base_lum = _luminance(str(source_palette.get(base_key, "")))
            variant_lum = _luminance(str(source_palette.get(variant, "")))
            if (base_lum is not None and variant_lum is not None
                    and abs(variant_lum - base_lum) > 0.5):
                factor = abs(factor) if variant_lum > base_lum else -abs(factor)
        out[variant] = blend(hex_color, factor)
    return out


# Roles that represent the same author-facing choice. They remain separate in
# saved palettes for backwards compatibility and direct fine tuning, but a
# simple-mode edit updates them together.
SIMPLE_LINKS: dict[str, tuple[str, ...]] = {
    "ACCENT": ("LINK_BLUE", "DROPDOWN_ARROW", "SCROLL_ACTIVE", "CHECK_FILL"),
    "TEXT_OK": ("TONE_GREEN",),
    "TEXT_ERR": ("TONE_RED",),
    "TEXT_WARN": ("TONE_FLAG",),
    "BTN_DANGER": ("RED_BTN",),
    "BTN_WARN": ("BTN_WARN_BROWN", "BTN_WARN_ORANGE"),
    "BTN_INFO": ("BTN_NEUTRAL",),
}


def derive_simple(base_key: str, hex_color: str,
                  source_palette: dict | None = None) -> dict[str, str]:
    """Expand a simple-mode edit to hover states and equivalent roles."""
    out = derive(base_key, hex_color, source_palette)
    for linked_key in SIMPLE_LINKS.get(base_key, ()):
        out.update(derive(linked_key, hex_color, source_palette))
    if base_key == "BG_SELECT":
        out["TEXT_ON_ACCENT"] = contrast_color(hex_color)
    return out


# --------------------------------------------------------------------------- #
# Group layout: [(section title, [(key, label), ...]), ...]
# Keys not listed here still appear under "Other" (built at runtime from the
# live palette) so a new palette key is never silently uneditable.
# --------------------------------------------------------------------------- #
GROUPS: list[tuple[str, list[tuple[str, str]]]] = [
    ("Surfaces and rows", [
        ("BG_DEEP", "Window background"),
        ("BG_PANEL", "Panels and dialogs"),
        ("BG_HEADER", "Toolbars and headers"),
        ("BG_LIST", "List / tree background"),
        ("BG_ROW", "List row background"),
        ("BG_ROW_ALT", "Alternate list row"),
        ("BG_ROW_HOVER", "Hovered list row"),
        ("BG_CARD", "Card background"),
    ]),
    ("Text", [
        ("TEXT_MAIN", "Primary text"),
        ("TEXT_DIM", "Secondary text"),
        ("TEXT_FAINT", "Faint text"),
        ("TEXT_WHITE", "White"),
    ]),
    ("Status text", [
        ("TEXT_OK", "Success text"),
        ("TEXT_ERR", "Error text"),
        ("TEXT_WARN", "Warning text"),
        ("TEXT_OK_BRIGHT", "Success text (bright)"),
        ("TEXT_ERR_BRIGHT", "Error text (bright)"),
        ("TEXT_WARN_BRIGHT", "Warning text (bright)"),
    ]),
    ("Accent and links", [
        ("ACCENT", "Accent"),
        ("ACCENT_HOV", "Accent hover"),
        ("TEXT_ON_ACCENT", "Text on accent / selection"),
        ("LINK_BLUE", "Hyperlink"),
        ("DROPDOWN_ARROW", "Dropdown arrow"),
    ]),
    ("Selection and focus", [
        ("BG_HOVER", "Hover highlight"),
        ("BG_SELECT", "Selection highlight"),
        ("HIGHLIGHT_DRAG", "Drag selection outline"),
    ]),
    ("Borders and separators", [
        ("BORDER", "Border"),
        ("BORDER_DIM", "Border (dim)"),
        ("BORDER_FAINT", "Border (faint)"),
        ("BG_SEP", "Separator row background"),
        ("TEXT_SEP", "Separator row text"),
    ]),
    ("Danger buttons", [
        ("BTN_DANGER", "Danger"),
        ("BTN_DANGER_HOV", "Danger hover"),
        ("RED_BTN", "Red (legacy)"),
        ("RED_HOV", "Red hover (legacy)"),
    ]),
    ("Success buttons", [
        ("BTN_SUCCESS", "Success"),
        ("BTN_SUCCESS_HOV", "Success hover"),
    ]),
    ("Warning buttons", [
        ("BTN_WARN", "Warning"),
        ("BTN_WARN_HOV", "Warning hover"),
        ("BTN_WARN_BROWN", "Warning (brown)"),
        ("BTN_WARN_BROWN_HOV", "Warning brown hover"),
        ("BTN_WARN_ORANGE", "Warning (orange)"),
        ("BTN_WARN_ORANGE_HOV", "Warning orange hover"),
    ]),
    ("Information buttons", [
        ("BTN_INFO", "Info"),
        ("BTN_INFO_HOV", "Info hover"),
        ("BTN_NEUTRAL", "Neutral"),
        ("BTN_NEUTRAL_HOV", "Neutral hover"),
    ]),
    ("Secondary buttons", [
        ("BTN_GREY", "Grey"),
        ("BTN_GREY_HOV", "Grey hover"),
    ]),
    ("Special accent buttons", [
        ("BTN_PURPLE", "Purple"),
        ("BTN_PURPLE_HOV", "Purple hover"),
    ]),
    ("Scrollbars and checkboxes", [
        ("SCROLL_BG", "Scrollbar background"),
        ("SCROLL_TROUGH", "Scrollbar trough"),
        ("SCROLL_ACTIVE", "Scrollbar thumb (active)"),
        ("CHECK_FILL", "Checkbox fill (checked)"),
    ]),
    ("Icons and small highlights", [
        ("TONE_GREEN", "Green tone"),
        ("TONE_RED", "Red tone"),
        ("TONE_BLUE", "Blue tone"),
        ("TONE_CYAN", "Cyan tone"),
        ("TONE_BLUE_SOFT", "Soft blue tone"),
    ]),
    ("Tinted content rows", [
        ("BG_GREEN_ROW", "Green row"),
        ("BG_GREEN_DEEP", "Green (deep)"),
        ("BG_RED_DEEP", "Red (deep)"),
        ("BG_ORANGE_DEEP", "Orange (deep)"),
        ("BG_GREEN_TEXT", "Green tint text"),
        ("BG_RED_TEXT", "Red tint text"),
    ]),
    ("Required and optional mods", [
        ("BG_MOD_REQ", "Required mod"),
        ("BG_MOD_OPT", "Optional mod"),
    ]),
    ("Notifications and queues", [
        ("STATUS_ERR_BRIGHT", "Error (bright)"),
        ("STATUS_BADGE_RED", "Badge red"),
        ("STATUS_QUEUED", "Queued"),
    ]),
    ("Plugin cycle", [
        ("PLUGIN_CYCLE_ERR_BG", "Cycle error row (bg)"),
        ("PLUGIN_CYCLE_ERR_FG", "Cycle error row (text)"),
        ("PLUGIN_CYCLE_OK_BG", "Cycle ok row (bg)"),
        ("PLUGIN_CYCLE_OK_FG", "Cycle ok row (text)"),
        ("PLUGIN_CYCLE_WARN_BG", "Cycle warn row (bg)"),
        ("PLUGIN_CYCLE_WARN_FG", "Cycle warn row (text)"),
        ("PLUGIN_CYCLE_ANCHOR", "Cycle anchor"),
        ("PLUGIN_CYCLE_LINK", "Cycle link"),
    ]),
    ("File conflicts", [
        ("FILE_WIN", "File winning"),
        ("FILE_LOSE", "File overridden"),
        ("FILE_DIM", "File dim"),
        ("FILE_ANCHOR", "File anchor"),
    ]),
    ("Conflict and requirement highlights", [
        ("CONFLICT_HL_WIN", "Conflict row - winning"),
        ("CONFLICT_HL_LOSE", "Conflict row - overridden"),
        ("CONFLICT_HL_ANCHOR", "Conflict row - anchor"),
        ("REQ_HL_REQUIRES", "Requirement row - requires"),
        ("REQ_HL_REQUIRED_BY", "Requirement row - required by"),
    ]),
    ("Framework status", [
        ("FRAMEWORK_INSTALLED_BG", "Installed (bg)"),
        ("FRAMEWORK_INSTALLED_FG", "Installed (text)"),
        ("FRAMEWORK_STAGED_BG", "Staged (bg)"),
        ("FRAMEWORK_STAGED_FG", "Staged (text)"),
        ("FRAMEWORK_DISABLED_BG", "Disabled (bg)"),
        ("FRAMEWORK_DISABLED_FG", "Disabled (text)"),
        ("FRAMEWORK_MISSING_BG", "Missing (bg)"),
        ("FRAMEWORK_MISSING_FG", "Missing (text)"),
    ]),
    ("Mod list separator bands", [
        ("OVERWRITE_SEP_BG", "Overwrite band (bg)"),
        ("OVERWRITE_SEP_FG", "Overwrite band (text)"),
        ("ROOT_SEP_BG", "Root Folder band (bg)"),
        ("ROOT_SEP_FG", "Root Folder band (text)"),
    ]),
]

# The default editor deliberately stays small. These are semantic choices a
# theme author can understand without knowing which individual widget consumes
# a palette role. Closely related implementation roles are updated through
# SIMPLE_LINKS / DERIVE above.
SIMPLE_GROUPS: list[tuple[str, list[tuple[str, str]]]] = [
    ("Surfaces", [
        ("BG_DEEP", "Window background"),
        ("BG_PANEL", "Panels and dialogs"),
        ("BG_HEADER", "Toolbars and headers"),
        ("BG_LIST", "List / tree background"),
        ("BG_ROW", "List row background"),
    ]),
    ("Text", [
        ("TEXT_MAIN", "Primary text"),
        ("TEXT_DIM", "Secondary text"),
        ("TEXT_OK", "Success text"),
        ("TEXT_WARN", "Warning text"),
        ("TEXT_ERR", "Error text"),
    ]),
    ("Accent and selection", [
        ("ACCENT", "Accent, links and controls"),
        ("BG_SELECT", "Selected rows"),
        ("BORDER", "Borders and dividers"),
    ]),
    ("Action buttons", [
        ("BTN_SUCCESS", "Confirm / install"),
        ("BTN_DANGER", "Delete / remove"),
        ("BTN_WARN", "Warning / update"),
        ("BTN_INFO", "Info / select"),
        ("BTN_GREY", "Secondary action"),
    ]),
]

SIMPLE_GROUP_DESCRIPTIONS: dict[str, str] = {
    "Surfaces": "The main layers of the app, from the window to list rows.",
    "Text": "General text plus the three semantic status colours.",
    "Accent and selection": "Brand colour, selected rows, focus controls and dividers.",
    "Action buttons": "Button colours are shared by actions with the same meaning.",
}

# These historical palette roles are retained in theme files so older custom
# themes remain loadable, but the Qt app never consumes them. Showing them in
# the editor suggested an effect that did not exist. Preview-only samples are
# included here as they likewise do not style any real application screen.
EDITOR_HIDDEN_KEYS: set[str] = {
    "BG_BLUE_DEEP", "BG_BLUE_TEXT", "BG_BTN_SAVE", "BG_CARD_ALT",
    "BG_DARK_BLUE", "BG_DARK_GREEN", "BG_ENTRY", "BG_HOVER_ROW",
    "BG_ORANGE_TEXT", "BG_OVERLAY_DEEP", "BG_OVERLAY_ERR", "BG_SELECT_BAR",
    "BTN_CANCEL", "BTN_CANCEL_HOV",
    "BTN_DANGER_ALT", "BTN_DANGER_ALT_HOV",
    "BTN_DANGER_DEEP", "BTN_DANGER_DEEP_HOV",
    "BTN_GREY_ALT", "BTN_GREY_ALT_HOV",
    "BTN_INFO_DEEP", "BTN_INFO_DEEP_HOV",
    "BTN_SUCCESS_ALT", "BTN_SUCCESS_ALT_HOV",
    "BTN_SUCCESS_DEEP", "BTN_SUCCESS_DEEP_HOV",
    "BTN_WARN_DEEP", "BTN_WARN_DEEP_HOV",
    "STATUS_BADGE_GREEN", "STATUS_DL_GREEN", "STATUS_SUCCESS_SOLID",
    "TAG_BSA", "TAG_BSA_ALT", "TAG_BUNDLED_BG", "TAG_BUNDLED_FG",
    "TAG_FOLDER", "TAG_INI_PROFILE", "TAG_INSTALLED_BG", "TAG_UNORDERED_FG",
    "TEXT_BLACK", "TEXT_CARD", "TEXT_CARD_DIM", "TEXT_CARD_MED",
    "TEXT_MUTED", "TEXT_TREE_FG", "TONE_FLAG",
}

# One-line "where does this show up" hint per section, rendered under the group
# title in the editor so it's obvious what each block of colours affects.
GROUP_DESCRIPTIONS: dict[str, str] = {
    "Surfaces and rows": "Window, panel, card, list and row backgrounds.",
    "Text": "Primary, secondary and faint text used throughout the app.",
    "Status text": "Success, warning and error messages shown on neutral backgrounds.",
    "Accent and links": "Brand accent, contrasting text, hyperlinks and control glyphs.",
    "Selection and focus": "Hover, selected-row and drag-selection colours.",
    "Borders and separators": "Frames, divider lines and separator rows.",
    "Danger buttons": "Delete, remove and other destructive actions.",
    "Success buttons": "Install, confirm, Done and Play actions.",
    "Warning buttons": "Update, reinstall and cautionary actions.",
    "Information buttons": "Select, Groups, Plugin Rules and similar actions.",
    "Secondary buttons": "View and other low-emphasis actions.",
    "Special accent buttons": "Special-purpose accent buttons such as Ko-Fi.",
    "Scrollbars and checkboxes": "Scrollbar track/thumb and checked-box fill.",
    "Icons and small highlights": "Shared tones used by icons, flags and file-tree markers.",
    "Tinted content rows": "Coloured information rows and their foreground text.",
    "Required and optional mods": "Required/optional indicators in collection views.",
    "Notifications and queues": "Error badges, notifications and queued states.",
    "Plugin cycle": "Cycle status rows and before/after rule keywords.",
    "File conflicts": "Winning, overridden, inactive and anchor files.",
    "Conflict and requirement highlights": "Related mod rows highlighted across Mods, Plugins and Data.",
    "Framework status": "Installed, staged, disabled and missing framework banners.",
    "Mod list separator bands": "Pinned Overwrite and Root Folder rows.",
}

# Shared role metadata lets the preview use the exact same names as the editor.
ROLE_LABELS: dict[str, str] = {
    key: label for _title, keys in GROUPS for key, label in keys
}
ROLE_GROUPS: dict[str, str] = {
    key: title for title, keys in GROUPS for key, _label in keys
}


def role_label(key: str) -> str:
    return ROLE_LABELS.get(key, key)


def role_group(key: str) -> str:
    return ROLE_GROUPS.get(key, "Other")


# Hidden roles are known schema keys rather than newly introduced "Other" keys.
_KNOWN_KEYS: set[str] = set(ROLE_LABELS) | EDITOR_HIDDEN_KEYS


def is_editable_value(value) -> bool:
    """True for plain ``#rrggbb`` string values (skip CTk (light,dark) tuples
    and any non-hex entries the editor can't render as a single swatch)."""
    return isinstance(value, str) and value.startswith("#") and len(value) in (7,)


def grouped_for_palette(palette: dict) -> list[tuple[str, list[tuple[str, str]]]]:
    """Return implemented fine-tuning roles present in *palette*.

    A trailing "Other" group keeps newly introduced editable keys discoverable;
    known legacy/no-op roles are intentionally excluded.
    """
    result: list[tuple[str, list[tuple[str, str]]]] = []
    for title, keys in GROUPS:
        present = [(k, label) for k, label in keys
                   if k in palette and k not in EDITOR_HIDDEN_KEYS]
        if present:
            result.append((title, present))
    extras = [(k, k) for k, v in palette.items()
              if (k not in _KNOWN_KEYS and k not in EDITOR_HIDDEN_KEYS
                  and is_editable_value(v))]
    if extras:
        result.append(("Other", sorted(extras)))
    return result


def simple_grouped_for_palette(
        palette: dict) -> list[tuple[str, list[tuple[str, str]]]]:
    """Return the compact semantic palette filtered to available keys."""
    result: list[tuple[str, list[tuple[str, str]]]] = []
    for title, keys in SIMPLE_GROUPS:
        present = [(key, label) for key, label in keys if key in palette]
        if present:
            result.append((title, present))
    return result
