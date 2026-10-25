from __future__ import annotations

from dataclasses import dataclass
import re


def QT_TRANSLATE_NOOP(_context, source):
    return source


@dataclass(frozen=True)
class ShortcutDefinition:
    action_id: str
    label: str
    default: str


SHORTCUT_DEFINITIONS = (
    ShortcutDefinition("rename", QT_TRANSLATE_NOOP(
        "ShortcutEditor", "Rename selected mod or separator"), "F2"),
    ShortcutDefinition("refresh", QT_TRANSLATE_NOOP(
        "ShortcutEditor", "Refresh mod list"), "F5"),
    ShortcutDefinition("deploy", QT_TRANSLATE_NOOP(
        "ShortcutEditor", "Deploy mods"), "Ctrl+D"),
    ShortcutDefinition("install_mod", QT_TRANSLATE_NOOP(
        "ShortcutEditor", "Install a mod"), "Ctrl+M"),
    ShortcutDefinition("create_empty_mod", QT_TRANSLATE_NOOP(
        "ShortcutEditor", "Create an empty mod"), "Ctrl+N"),
    ShortcutDefinition("restore", QT_TRANSLATE_NOOP(
        "ShortcutEditor", "Restore game files"), "Ctrl+R"),
    ShortcutDefinition("open_settings", QT_TRANSLATE_NOOP(
        "ShortcutEditor", "Open Settings"), "Ctrl+S"),
    ShortcutDefinition("focus_search", QT_TRANSLATE_NOOP(
        "ShortcutEditor", "Focus search"), "Ctrl+F"),
    ShortcutDefinition("select_all", QT_TRANSLATE_NOOP(
        "ShortcutEditor", "Select all in the active group"), "Ctrl+A"),
    ShortcutDefinition("move_up", QT_TRANSLATE_NOOP(
        "ShortcutEditor", "Move selection up"), "Alt+Up"),
    ShortcutDefinition("move_down", QT_TRANSLATE_NOOP(
        "ShortcutEditor", "Move selection down"), "Alt+Down"),
    ShortcutDefinition("remove", QT_TRANSLATE_NOOP(
        "ShortcutEditor", "Remove selected mods"), "Delete"),
    ShortcutDefinition("toggle_selected", QT_TRANSLATE_NOOP(
        "ShortcutEditor", "Enable or disable selection"), "Enter"),
    ShortcutDefinition("scroll_top", QT_TRANSLATE_NOOP(
        "ShortcutEditor", "Scroll active list to the top"), "Home"),
    ShortcutDefinition("scroll_bottom", QT_TRANSLATE_NOOP(
        "ShortcutEditor", "Scroll active list to the bottom"), "End"),
    ShortcutDefinition("toggle_separators", QT_TRANSLATE_NOOP(
        "ShortcutEditor", "Expand or collapse all separators"), "Shift+E"),
    ShortcutDefinition("toggle_filters", QT_TRANSLATE_NOOP(
        "ShortcutEditor", "Show or hide active filters"), "Shift+F"),
    ShortcutDefinition("open_mod_page", QT_TRANSLATE_NOOP(
        "ShortcutEditor", "Open a mod's source page"), "Mouse 3"),
    ShortcutDefinition("browser_back", QT_TRANSLATE_NOOP(
        "ShortcutEditor", "Browser back"), "Mouse 4"),
    ShortcutDefinition("browser_forward", QT_TRANSLATE_NOOP(
        "ShortcutEditor", "Browser forward"), "Mouse 5"),
)

MOUSE_ACTION_IDS = (
    "open_mod_page", "browser_back", "browser_forward",
)

_KEY_NAMES = (
    'Space|Tab|Backtab|Backspace|Return|Enter|Ins|Del|Pause|Print|SysReq|Clear|Home|End|Left|Up|Right|'
    'Down|PgUp|PgDown|CapsLock|NumLock|ScrollLock|F1|F2|F3|F4|F5|F6|F7|F8|F9|F10|F11|F12|F13|F14|F15|F16|'
    'F17|F18|F19|F20|F21|F22|F23|F24|F25|F26|F27|F28|F29|F30|F31|F32|F33|F34|F35|Menu|Help|Code input|'
    'Multiple Candidate|Previous Candidate|Kanji|Muhenkan|Henkan|Romaji|Hiragana|Katakana|'
    'Hiragana Katakana|Zenkaku|Hankaku|Zenkaku Hankaku|Touroku|Massyo|Kana Lock|Kana Shift|Eisu Shift|'
    'Eisu toggle|Hangul|Hangul Start|Hangul End|Hangul Hanja|Hangul Jamo|Hangul Romaja|Hangul Jeonja|'
    'Hangul Banja|Hangul PreHanja|Hangul PostHanja|Hangul Special|Back|Forward|Stop|Refresh|Volume Down|'
    'Volume Mute|Volume Up|Bass Boost|Bass Up|Bass Down|Treble Up|Treble Down|Media Play|Media Stop|'
    'Media Previous|Media Next|Media Record|Media Pause|Toggle Media Play/Pause|Home Page|Favorites|'
    'Search|Standby|Open URL|Launch Mail|Launch Media|Launch (0)|Launch (1)|Launch (2)|Launch (3)|'
    'Launch (4)|Launch (5)|Launch (6)|Launch (7)|Launch (8)|Launch (9)|Launch (A)|Launch (B)|Launch (C)|'
    'Launch (D)|Launch (E)|Launch (F)|Monitor Brightness Up|Monitor Brightness Down|Keyboard Light On/Off|'
    'Keyboard Brightness Up|Keyboard Brightness Down|Power Off|Wake Up|Eject|Screensaver|WWW|Memo|'
    'LightBulb|Shop|History|Add Favorite|Hot Links|Adjust Brightness|Finance|Community|Media Rewind|'
    'Back Forward|Application Left|Application Right|Book|CD|Calculator|To-do list|Clear Grab|Close|Copy|'
    'Cut|Display|DOS|Documents|Spreadsheet|Browser|Game|Go|iTouch|Logoff|Market|Meeting|Keyboard Menu|'
    'Menu PB|My Sites|News|Home Office|Option|Paste|Phone|Calendar|Reply|Reload|Rotate Windows|'
    'Rotation PB|Rotation KB|Save|Send|Spellchecker|Split Screen|Support|Task Panel|Terminal|Tools|Travel|'
    'Video|Word Processor|XFer|Zoom In|Zoom Out|Away|Messenger|WebCam|Mail Forward|Pictures|Music|Battery|'
    'Bluetooth|Wireless|Ultra Wide Band|Media Fast Forward|Audio Repeat|Audio Random Play|Subtitle|'
    'Audio Cycle Track|Time|Hibernate|View|Top Menu|Power Down|Suspend|Adjust contrast|Launch (G)|'
    'Launch (H)|Touchpad Toggle|Touchpad On|Touchpad Off|Microphone Mute|Red|Green|Yellow|Blue|Channel Up|'
    'Channel Down|Guide|Info|Settings|Microphone Volume Up|Microphone Volume Down|Keyboard|New|Open|Find|'
    'Undo|Redo|Select|Yes|No|Cancel|Printer|Execute|Sleep|Play|Zoom|Exit|Context1|Context2|Context3|'
    'Context4|Call|Hangup|Flip|Toggle Call/Hangup|Voice Dial|Last Number Redial|Camera Shutter|'
    'Camera Focus|'
).rstrip("|").split("|")
_KEYS = {name.casefold(): name for name in _KEY_NAMES}
_KEYS.update({"insert": "Ins", "delete": "Del", "page up": "PgUp", "page down": "PgDown",
              "print screen": "Print"})
_MODIFIERS = ("Meta", "Ctrl", "Alt", "Shift")


def binding_parts(sequence):
    if not isinstance(sequence, str):
        return None
    text = "Space" if sequence == " " else sequence.strip(" \t")
    modifiers = set()
    while "+" in text:
        head, rest = text.split("+", 1)
        modifier = next((name for name in _MODIFIERS if name.casefold() == head.strip().casefold()), None)
        if modifier is None:
            break
        modifiers.add(modifier)
        text = rest.strip(" \t")
    ordered = tuple(name for name in _MODIFIERS if name in modifiers)
    mouse = re.fullmatch(r"Mouse ([0-9]{1,2})", text)
    if mouse is not None:
        number = int(mouse[1])
        return ("mouse", ordered, f"Mouse {number}") if 3 <= number <= 27 else None
    key = _KEYS.get(text.casefold())
    if (key is None and len(text) == 1 and ord(text) >= 32
            and not 0x7f <= ord(text) <= 0x9f and not 0xd800 <= ord(text) <= 0xdfff):
        upper = text.upper()
        key = upper if len(upper) == 1 else text
    return ("keyboard", ordered, key) if key else None


def normalise_binding(sequence):
    parts = binding_parts(sequence)
    return "+".join((*parts[1], parts[2])) if parts is not None else ""


def binding_identity(sequence):
    parts = binding_parts(sequence)
    if parts is None:
        return None
    kind, modifiers, key = parts
    return kind, modifiers, "Return" if kind == "keyboard" and key == "Enter" else key


def plugin_shortcut_id(value):
    return isinstance(value, str) and re.fullmatch(
        r"plugin/[a-z0-9][a-z0-9_.-]{0,99}/[a-z0-9][a-z0-9_.-]{0,99}", value) is not None


def shortcut_definitions():
    from amethyst_plugins.catalog import discover
    plugins, _errors = discover(None)
    return (*SHORTCUT_DEFINITIONS, *(
        ShortcutDefinition(f"plugin/{plugin.id}/{command.id}",
                           f"Plugins · {plugin.name}: {command.label}", "")
        for plugin in plugins for command in plugin.commands))


def shortcut_defaults():
    return {item.action_id: normalise_binding(item.default) for item in shortcut_definitions()}


def _duplicate_groups(values: dict[str, str]) -> list[list[str]]:
    by_sequence: dict[tuple, list[str]] = {}
    for action_id, sequence in values.items():
        identity = binding_identity(sequence)
        if identity is not None:
            by_sequence.setdefault(identity, []).append(action_id)
    return [ids for ids in by_sequence.values() if len(ids) > 1]


def _validated_shortcuts(overrides: dict[str, str]) -> dict[str, str]:
    defaults = shortcut_defaults()
    values = dict(defaults)
    for action_id, raw in overrides.items():
        if action_id in values:
            sequence = normalise_binding(raw)
            if sequence:
                values[action_id] = sequence

    # A hand-edited or old config must not create ambiguous QShortcuts. Prefer
    # an action still using its default and roll colliding overrides back.
    for _attempt in range(len(values) + 1):
        groups = _duplicate_groups(values)
        if not groups:
            return values
        changed = False
        for action_ids in groups:
            for action_id in action_ids:
                if values[action_id] != defaults[action_id]:
                    values[action_id] = defaults[action_id]
                    changed = True
        if not changed:
            break
    return dict(defaults)


def load_shortcuts() -> dict[str, str]:
    """Load valid overrides on top of the built-in defaults."""
    from Utils.ui.config import load_shortcut_overrides

    try:
        overrides = load_shortcut_overrides()
    except Exception:
        overrides = {}
    return _validated_shortcuts(overrides)
