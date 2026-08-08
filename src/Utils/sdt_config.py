"""
GUI-neutral SSE Display Tweaks config schema + INI parse/render/load/save.

Moved out of wizards/sse_display_tweaks.py (which imports customtkinter) so
the Qt config-editor view can share it. The schema, parse_ini/render_ini,
the documented default template and the layered value loader all live here;
the filemap lookup reuses Utils.wizard_gates.filemap_find.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from Games.base_game import BaseGame

MOD_NAME = "SSE Display Tweaks ini"
REL_INI_PATH = "SKSE/Plugins/SSEDisplayTweaks.ini"
REL_DLL_PATH = "SKSE/Plugins/SSEDisplayTweaks.dll"


# ---------------------------------------------------------------------------
# Setting schema
# ---------------------------------------------------------------------------

class Setting:
    """One configurable ini key.

    kind: "bool" | "enum" | "int" | "float" | "str"
    """

    __slots__ = ("section", "key", "kind", "default", "desc", "choices",
                 "enabled_by_default")

    def __init__(self, section, key, kind, default, desc, *,
                 choices=None, enabled_by_default=True):
        self.section = section
        self.key = key
        self.kind = kind
        self.default = default
        self.desc = desc
        self.choices = choices
        self.enabled_by_default = enabled_by_default

    @property
    def id(self) -> tuple[str, str]:
        return (self.section, self.key)


_LOG_LEVELS = ["debug", "verbose", "message", "warning", "error", "fatal"]
_SWAP_EFFECTS = ["auto", "discard", "sequential", "flip_sequential", "flip_discard"]
_SCALING_MODES = ["unspecified", "centered", "stretched"]

SCHEMA: list[Setting] = [
    # ---- [Main] ----
    Setting("Main", "LogLevel", "enum", "debug",
            "Level of information printed to the log.", choices=_LOG_LEVELS),
    Setting("Main", "AdjustGameSettings", "bool", "true",
            "Auto-adjust iFPSClamp / physics step settings in-memory. Leave on "
            "unless you know what you're doing."),

    # ---- [Render] ----
    Setting("Render", "Fullscreen", "bool", "false",
            "true = exclusive fullscreen, false = windowed/borderless. Overrides "
            "'bFull Screen' in SkyrimPrefs.ini."),
    Setting("Render", "Borderless", "bool", "true",
            "Only when Fullscreen=false. true = borderless fullscreen, false = "
            "windowed."),
    Setting("Render", "BorderlessUpscale", "bool", "false",
            "Stretch the window across the whole screen in borderless mode. Best "
            "with a flip SwapEffect."),
    Setting("Render", "Resolution", "str", "1920x1080",
            "Game resolution in windowed/borderless mode. Overrides iSize W/H.",
            enabled_by_default=False),
    Setting("Render", "ResolutionScale", "float", "0.75",
            "Scale the resolution in windowed/borderless mode.",
            enabled_by_default=False),
    Setting("Render", "DisableBufferResizing", "bool", "false",
            "Disable swap-chain buffer resizing on window resize. Enable if ENB "
            "has effect issues when upscaling."),
    Setting("Render", "DisableTargetResizing", "bool", "false",
            "Disable render-target resizing when window dimensions change.",
            enabled_by_default=False),
    Setting("Render", "EnableVSync", "bool", "true",
            "Enable/disable VSync. To disable VSync in borderless, also set "
            "EnableTearing=true. Overrides iVSyncPresentInterval."),
    Setting("Render", "VSyncPresentInterval", "int", "1",
            "VSync present interval (1-4). Only when EnableVSync=true. Leave at 1; "
            "higher values reduce framerate."),
    Setting("Render", "EnableTearing", "bool", "false",
            "Required to disable VSync in borderless/windowed (flip SwapEffect "
            "only). ENB: disable if enblocal ForceVSync=true."),
    Setting("Render", "SwapBufferCount", "int", "0",
            "Buffers in the swap chain incl. front buffer (1-8, 0=auto). Borderless "
            "flip needs at least 2."),
    Setting("Render", "SwapEffect", "enum", "auto",
            "How the presentation buffer is handled. Leave on auto. Don't use flip "
            "in exclusive fullscreen.", choices=_SWAP_EFFECTS),
    Setting("Render", "MaxFrameLatency", "int", "1",
            "Frames allowed to be queued (1-16, 0=default). Above 2 can hurt "
            "performance."),
    Setting("Render", "ScalingMode", "enum", "unspecified",
            "Scaling in exclusive fullscreen mode.", choices=_SCALING_MODES),
    Setting("Render", "MaximumRefreshRate", "int", "300",
            "Max monitor refresh rate requested in exclusive fullscreen. Upper "
            "limit only, not a fixed value."),
    Setting("Render", "FramerateLimit", "int", "300",
            "General framerate limit (replaces the built-in limiter). Bethesda "
            "default: 60."),
    Setting("Render", "FramerateLimitMode", "int", "1",
            "0 = before presentation (consistent frametimes), 1 = after (input "
            "latency)."),
    Setting("Render", "UIFramerateLimit", "int", "0",
            "UI framerate limit in paused menus. -1 disables the limit explicitly."),
    Setting("Render", "UIFramerateLimitVSyncOff", "bool", "true",
            "Apply the UI framerate limit when VSync is off."),
    Setting("Render", "UIFramerateLimitMap", "int", "0",
            "Framerate limit for the map (adjusts map movement speed)."),
    Setting("Render", "UIFramerateLimitMapVSyncOff", "bool", "false",
            "Apply the map limit when VSync is off."),
    Setting("Render", "UIFramerateLimitInventory", "int", "0",
            "Framerate limit for inventory/magic/gift/barter/container/favorites."),
    Setting("Render", "UIFramerateLimitInventoryVSyncOff", "bool", "true",
            "Apply the inventory limit when VSync is off."),
    Setting("Render", "UIFramerateLimitJournal", "int", "0",
            "Framerate limit for the journal."),
    Setting("Render", "UIFramerateLimitJournalVSyncOff", "bool", "true",
            "Apply the journal limit when VSync is off."),
    Setting("Render", "UIFramerateLimitCustom", "int", "0",
            "Framerate limit for custom menus."),
    Setting("Render", "UIFramerateLimitCustomVSyncOff", "bool", "true",
            "Apply the custom-menu limit when VSync is off."),
    Setting("Render", "UIFramerateLimitMain", "int", "0",
            "Framerate limit for the main menu. Recommended 60 with VSyncOff=false."),
    Setting("Render", "UIFramerateLimitMainVSyncOff", "bool", "true",
            "Apply the main-menu limit when VSync is off."),
    Setting("Render", "UIFramerateLimitRace", "int", "0",
            "Framerate limit for the race menu."),
    Setting("Render", "UIFramerateLimitRaceVSyncOff", "bool", "true",
            "Apply the race-menu limit when VSync is off."),
    Setting("Render", "UIFramerateLimitPerk", "int", "0",
            "Framerate limit for the perk tree."),
    Setting("Render", "UIFramerateLimitPerkVSyncOff", "bool", "true",
            "Apply the perk-tree limit when VSync is off."),
    Setting("Render", "UIFramerateLimitBook", "int", "0",
            "Framerate limit while reading books."),
    Setting("Render", "UIFramerateLimitBookVSyncOff", "bool", "true",
            "Apply the book limit when VSync is off."),
    Setting("Render", "UIFramerateLimitLockpick", "int", "0",
            "Framerate limit while picking locks."),
    Setting("Render", "UIFramerateLimitLockpickVSyncOff", "bool", "true",
            "Apply the lockpick limit when VSync is off."),
    Setting("Render", "UIFramerateLimitConsole", "int", "0",
            "Framerate limit for the console."),
    Setting("Render", "UIFramerateLimitConsoleVSyncOff", "bool", "true",
            "Apply the console limit when VSync is off."),
    Setting("Render", "UIFramerateLimitTween", "int", "0",
            "Framerate limit for the tween menu."),
    Setting("Render", "UIFramerateLimitTweenVSyncOff", "bool", "true",
            "Apply the tween-menu limit when VSync is off."),
    Setting("Render", "UIFramerateLimitSleepWait", "int", "0",
            "Framerate limit for the sleep/wait menu."),
    Setting("Render", "UIFramerateLimitSleepWaitVSyncOff", "bool", "true",
            "Apply the sleep/wait limit when VSync is off."),
    Setting("Render", "LoadingScreenFramerateLimit", "int", "60",
            "Framerate limit while loading. Keep at 60; uncapping can cause ILS or "
            "crashes. Don't exceed 120."),
    Setting("Render", "LoadingScreenFramerateLimitVSyncOff", "bool", "false",
            "Apply the loading-screen limit when VSync is off."),
    Setting("Render", "LoadingScreenLimitExtraTimePostLoad", "int", "2",
            "Extra seconds to keep the loading limit active after a load (post)."),
    Setting("Render", "LoadingScreenLimitExtraTime", "int", "2",
            "Extra seconds to keep the loading limit active."),

    # ---- [HAVOK] ----
    Setting("HAVOK", "Enabled", "bool", "true",
            "HAVOK master switch. false falls back to Skyrim.ini HAVOK settings."),
    Setting("HAVOK", "DynamicMaxTimeScaling", "bool", "true",
            "Adjust fMaxTime / fMaxTimeComplex per-frame based on framerate. "
            "Recommended on."),
    Setting("HAVOK", "MinimumFramerate", "int", "60",
            "fMaxTime values not calculated below this. Lower may help if you "
            "struggle to hit 60. Default 60."),
    Setting("HAVOK", "MaximumFramerate", "int", "300",
            "fMaxTime values not calculated above this. Set 0 to auto-determine, "
            "or above your max in-game framerate."),
    Setting("HAVOK", "MaxTimeComplexOffset", "int", "30",
            "Negative offset of fMaxTimeComplex relative to fMaxTime (0-30)."),
    Setting("HAVOK", "PhysicsDamagePatch", "bool", "true",
            "Scale damage from physics-object hits by frametime."),
    Setting("HAVOK", "PhysicsDamageMult", "float", "1",
            "Multiplier for physics-object damage (0-1). 0 disables it."),
    Setting("HAVOK", "PerformanceMode", "bool", "false",
            "Use cheaper 'complex scene' physics everywhere. Only for CPU-bound "
            "low-end systems."),
    Setting("HAVOK", "OSDStatsEnabled", "bool", "false",
            "Add fMaxTime / fMaxTimeComplex to the OSD."),

    # ---- [Controls] ----
    Setting("Controls", "ThirdPersonMovementFix", "bool", "true",
            "Fix being unable to move in third person at high framerates."),
    Setting("Controls", "MovementThreshold", "float", "0.25",
            "Internal movement/damping variable (0.01-5). Lower helps at low "
            "speeds / high fps. Only when ThirdPersonMovementFix=true."),
    Setting("Controls", "SittingHorizontalLookSensitivityFix", "bool", "true",
            "Untie first-person horizontal look sensitivity from framerate while "
            "sitting."),
    Setting("Controls", "MapMoveKeyboardSpeedFix", "bool", "true",
            "Untie & normalize map keyboard movement speed from framerate."),
    Setting("Controls", "MapMoveKeyboardSpeedMult", "float", "1",
            "Map keyboard movement speed multiplier (-20 to 20). Only when "
            "MapMoveKeyboardSpeedFix=true."),
    Setting("Controls", "AutoVanityCameraSpeedFix", "bool", "true",
            "Untie auto vanity-camera rotation speed from framerate."),
    Setting("Controls", "DialogueLookSpeedFix", "bool", "true",
            "Untie dialogue look speed from framerate."),
    Setting("Controls", "DialogueLookSmoothEdge", "bool", "false",
            "Ramp look speed up as the cursor nears the screen edge."),
    Setting("Controls", "GamepadCursorSpeedFix", "bool", "true",
            "Untie gamepad cursor speed from framerate and timescale."),
    Setting("Controls", "LockpickRotationSpeedFix", "bool", "true",
            "Untie lockpick mouse rotation speed from framerate."),
    Setting("Controls", "FreeCameraVerticalSensitivityFix", "bool", "true",
            "Untie free-camera vertical sensitivity from framerate."),
    Setting("Controls", "FreeCameraMovementSpeedFix", "bool", "true",
            "Untie free-camera movement speed from framerate."),
    Setting("Controls", "VerticalLookSensitivityFix", "bool", "false",
            "Untie vertical look sensitivity from framerate (AE only / EngineFixes)."),
    Setting("Controls", "SlowTimeCameraMovementFix", "bool", "false",
            "Fix camera sensitivity during slow time (AE only / EngineFixes)."),

    # ---- [Window] ----
    Setting("Window", "LockCursor", "bool", "true",
            "Lock the mouse cursor inside the game window (multi-monitor fix)."),
    Setting("Window", "ForceMinimize", "bool", "false",
            "Minimize the window when it loses focus. Avoid with ENB/ReShade."),
    Setting("Window", "DisableProcessWindowsGhosting", "bool", "true",
            "Disable window ghosting. Useful in windowed/borderless."),
    Setting("Window", "AutoCenter", "bool", "false",
            "Auto-center the window on its monitor (windowed mode)."),
    Setting("Window", "OffsetX", "int", "0",
            "Window X position offset relative to the primary monitor."),
    Setting("Window", "OffsetY", "int", "0",
            "Window Y position offset relative to the primary monitor."),

    # ---- [Papyrus] ----
    Setting("Papyrus", "DynamicUpdateBudget", "bool", "false",
            "Scale script execution time per cycle by framerate (less budget at "
            "higher fps)."),
    Setting("Papyrus", "UpdateBudgetBase", "float", "1.2",
            "Script time budget at/below 60 FPS (ms). Match Skyrim.ini "
            "fUpdateBudgetMS. Bethesda default 1.2."),
    Setting("Papyrus", "BudgetMaxFPS", "int", "300",
            "Budget not calculated beyond this (60-300). Set at/above your max "
            "framerate."),
    Setting("Papyrus", "SetExpressionOverridePatch", "bool", "true",
            "Fix a range check in Actor.SetExpressionOverride (Dialogue Anger "
            "expression)."),
    Setting("Papyrus", "OSDStatsEnabled", "bool", "false",
            "Add fUpdateBudgetMS to the OSD."),
    Setting("Papyrus", "OSDWarnVMOverstressed", "bool", "true",
            "Show a VM-overstressed indicator on the OSD under excessive script "
            "load."),

    # ---- [Miscellaneous] ----
    Setting("Miscellaneous", "SkipMissingPluginINI", "bool", "true",
            "Skip scanning for missing plugin .ini files. Can speed up startup "
            "with many plugins."),
    Setting("Miscellaneous", "LoadScreenFilter", "bool", "false",
            "Filter loadscreens by plugin name (see LoadScreenAllow/Block)."),
    Setting("Miscellaneous", "LoadScreenAllow", "str", "",
            "Comma-separated plugin names whose loadscreens are allowed."),
    Setting("Miscellaneous", "LoadScreenBlock", "str", "All",
            "Comma-separated plugin names to block (or 'All')."),
    Setting("Miscellaneous", "DisableWeatherLensFlare", "bool", "false",
            "Remove all lens flare from weather records (in-memory)."),
    Setting("Miscellaneous", "DisableActorFade", "bool", "false",
            "Disable actor fade when the camera intersects the body."),
    Setting("Miscellaneous", "DisablePlayerFade", "bool", "false",
            "Disable player fade when the camera intersects the body."),

    # ---- [OSD] ----
    Setting("OSD", "Enable", "bool", "false",
            "Enable the on-screen display."),
    Setting("OSD", "InitiallyOn", "bool", "true",
            "Show the OSD immediately on startup."),
    Setting("OSD", "Show", "str", "fps,vram",
            "Comma-separated stats: fps, bare_fps, frametime, bare_frametime, "
            "counter, vram, all."),
    Setting("OSD", "UpdateInterval", "float", "0.3",
            "How often the OSD updates (seconds)."),
    Setting("OSD", "ComboKey", "enum", "1",
            "OSD toggle modifier key. 1-8 = L/R Shift, Ctrl, Alt, Win.",
            choices=["1", "2", "3", "4", "5", "6", "7", "8"]),
    Setting("OSD", "ToggleKey", "str", "0xD2",
            "OSD toggle key (DX scan code). 0xD2 = Insert."),
    Setting("OSD", "Align", "enum", "1",
            "OSD alignment. 1=Top Left, 2=Top Right, 3=Bottom Left, 4=Bottom "
            "Right.", choices=["1", "2", "3", "4"]),
    Setting("OSD", "Offset", "str", "4 4",
            "OSD position offset (X Y)."),
    Setting("OSD", "Scale", "str", "1.0 0.9",
            "OSD font scale (X Y). Omit Y for uniform scaling."),
    Setting("OSD", "AutoScale", "bool", "true",
            "Adjust font scale based on the number of lines drawn."),
    Setting("OSD", "ScaleToWindow", "bool", "true",
            "Scale font size based on window size (constant at non-native "
            "resolutions)."),
    Setting("OSD", "FontFile", "str", "",
            "Custom OSD font bitmap (in Data/SKSE/Plugins/SDTFonts)."),
    Setting("OSD", "Color", "str", "255 255 255 255",
            "Font color (R G B A)."),
    Setting("OSD", "OutlineColor", "str", "0 0 0 255",
            "Font outline color (R G B A)."),
    Setting("OSD", "OutlineOffset", "int", "1",
            "Font outline offset."),
]

# Section order for rendering (matches the shipped ini).
SECTION_ORDER = ["Main", "Render", "HAVOK", "Controls", "Window", "Papyrus",
                 "Miscellaneous", "OSD"]

_SCHEMA_BY_ID = {s.id: s for s in SCHEMA}


# ---------------------------------------------------------------------------
# INI parse / render helpers
# ---------------------------------------------------------------------------


_SECTION_RE = re.compile(r"^\s*\[([^\]]+)\]\s*$")
# A setting line: an optional single '#' (disabled) immediately followed by the
# key - no space between. This distinguishes a commented-out setting
# ('#Key=value') from prose comment lines ('## ...', '#  ComboKey=1 and ...'),
# which always put whitespace after the '#'.
_SETTING_RE = re.compile(r"^(\s*)(#?)([A-Za-z][A-Za-z0-9_]*)\s*=(.*)$")



def parse_ini(text: str) -> dict[tuple[str, str], tuple[str, bool]]:
    """Parse an ini into ``{(section, key): (value, enabled)}``.

    ``enabled`` is False when the line is commented out (leading '#').
    The first occurrence of a key within a section wins.
    """
    result: dict[tuple[str, str], tuple[str, bool]] = {}
    section = ""
    for raw in text.splitlines():
        sec_m = _SECTION_RE.match(raw)
        if sec_m:
            section = sec_m.group(1).strip()
            continue
        m = _SETTING_RE.match(raw)
        if not m:
            continue
        commented, key, value = m.group(2), m.group(3), m.group(4).strip()
        ident = (section, key)
        if ident in result:
            continue
        result[ident] = (value, commented != "#")
    return result


def render_ini(base_text: str,
               values: dict[tuple[str, str], tuple[str, bool]]) -> str:
    """Rewrite ``base_text`` in place applying ``values``.

    Preserves all documentation comments and the original line-ending style.
    For each setting line present in the template, the value is updated and the
    line is commented ('#Key=value') when disabled. Settings in ``values`` that
    aren't already in the template are appended under their section.
    """
    newline = "\r\n" if "\r\n" in base_text else "\n"
    lines = base_text.split("\n")
    # Strip a trailing '\r' left by splitting on '\n' for CRLF files.
    lines = [ln[:-1] if ln.endswith("\r") else ln for ln in lines]

    seen: set[tuple[str, str]] = set()
    section = ""
    # Track the last line index that belongs to each section so we can append.
    section_last_idx: dict[str, int] = {}

    for i, raw in enumerate(lines):
        sec_m = _SECTION_RE.match(raw)
        if sec_m:
            section = sec_m.group(1).strip()
            section_last_idx[section] = i
            continue
        section_last_idx[section] = i
        m = _SETTING_RE.match(raw)
        if not m:
            continue
        key = m.group(3)
        ident = (section, key)
        if ident not in values:
            continue
        value, enabled = values[ident]
        indent = m.group(1)
        prefix = "" if enabled else "#"
        lines[i] = f"{indent}{prefix}{key}={value}"
        seen.add(ident)

    # Append any settings not present in the template, under their section.
    extras = [ident for ident in values if ident not in seen]
    if extras:
        # Group by section, append after the section's last known line.
        by_section: dict[str, list[tuple[str, str]]] = {}
        for ident in extras:
            by_section.setdefault(ident[0], []).append(ident)
        # Insert from the bottom up so earlier indices stay valid.
        for sec in sorted(by_section, key=lambda s: section_last_idx.get(s, len(lines)),
                          reverse=True):
            idx = section_last_idx.get(sec)
            block: list[str] = []
            if idx is None:
                # Section missing entirely - add a header at the end.
                block.append("")
                block.append(f"[{sec}]")
                idx = len(lines) - 1
            for (s_sec, key) in by_section[sec]:
                value, enabled = values[(s_sec, key)]
                prefix = "" if enabled else "#"
                block.append(f"{prefix}{key}={value}")
            lines[idx + 1:idx + 1] = block

    return newline.join(lines)


# ---------------------------------------------------------------------------
# Default ini template (shipped SSE Display Tweaks defaults, fully documented)
# ---------------------------------------------------------------------------

DEFAULT_INI = """[Main]

## Level of information printed to the log
#
#    debug
#    verbose
#    message
#    warning
#    error
#    fatal
#
LogLevel=debug

## Automatically adjust some settings which may cause issues.
#
#  Changes:
#
#    iFPSClamp=0
#    uMaxNumPhysicsStepsPerUpdate=3
#    uMaxNumPhysicsStepsPerUpdateComplex=1
#
#  Note: Values are modified in-memory and are not written to Skyrim.ini.
#        Physics values are not modified if HAVOK master switch is off.
#
#  Don't disable unless you know what you're doing.
#
AdjustGameSettings=true


[Render]

## Select the display mode.
#
#    true  - Exclusive fullscreen mode
#    false - Windowed mode
#
#  Note: This option overrides 'bFull Screen' in SkyrimPrefs.ini if uncommented.
#
Fullscreen=false

## Select windowed or borderless fullscreen mode. Only applies when Fullscreen=false.
#
#    true  - Borderless fullscreen
#    false - Windowed mode
#
#  Note: This option overrides 'bBorderless' in SkyrimPrefs.ini if uncommented.
#
Borderless=true

## Stretch game window across the entire screen in borderless fullscreen mode.
#
#  Use with a flip SwapEffect option for best results.
#
#  Note: For optimal performance and full feature support when upscaling, your
#        system must have windowed hardware composition support (check the log).
#        Windowed hardware composition only works with flip.
#
BorderlessUpscale=false

## Set the game resolution. Only applies in windowed and borderless fullscreen mode (when Fullscreen=false).
#
#  Provided for convenience. Easily scale or set the resolution in windowed/borderless fullsceen mode.
#
#  Note: These options override iSize W and iSize H in SkyrimPrefs.ini. They have no effect when commented out.
#
#Resolution=1920x1080
#ResolutionScale=0.75

## Disable swap chain buffer resizing when window dimensions change (e.g. when upscaling).
#
#  Enable if you're experiencing effect issues with ENB when upscaling.
#
DisableBufferResizing=false
#DisableTargetResizing=false

## Enable/disable VSync.
#
#  IMPORTANT: If you're using borderless mode and want to disable VSync, EnableTearing must be set to true.
#
#  Note: This option overrides 'iVSyncPresentInterval' in SkyrimPrefs.ini
#
EnableVSync=true

## VSync present interval, same as 'iVSyncPresentInterval' in SkyrimPrefs.ini. Only applies if EnableVSync=true.
#
#  If SwapEffect is 'discard' or 'sequential':
#    Synchronize presentation after the nth vertical blank.
#
#  If SwapEffect is 'flip_sequential':
#    Synchronize presentation for at least n vertical blanks.
#
#  Valid range: 1-4
#
#  It's recommended to leave this at the default value (1). Higher values effectively reduce framerate.
#  For example, a value of 2 at 60Hz would cut framerate in half.
#
VSyncPresentInterval=1

## Required for disabling V-Sync in borderless/windowed mode. Only works with a flip SwapEffect option.
#
#  ENB WARNING: Disable this if you have ForceVSync set to true in enblocal.ini, otherwise your game might freeze on startup.
#
EnableTearing=false

## Number of buffers in the swap chain, including the front buffer.
#
#  Valid range: 1-8
#
#  Set to 0 to select automatically.
#
#  Note: Borderless fullscreen with flip model requires at least 2, if the value is lower it will be adjusted automatically.
#
SwapBufferCount=0

## Determines how the presentation buffer is handled.
#
#  Valid options:
#
#     auto
#     discard
#     sequential
#     flip_sequential
#     flip_discard
#
#  Options starting with 'flip_' indicate DXGI flip model, which greatly improves borderless fullscreen performance.
#
#  WARNING: Don't use flip model in exclusive fullscreen mode, the game might freeze on start.
#
#  It's recommended to leave this on auto as the best option will be selected based on detected capabilities.
#
SwapEffect=auto

## Number of frames allowed to be queued for rendering.
#
#  Valid range: 1-16 (0 leaves it at default)
#
#  Bethesda default is 2 for SE, 1 for AE.
#
#  WARNING: Values above 2 could have a negative impact on overall performance.
#
MaxFrameLatency=1

## Determines how scaling is done in exclusive fullscreen mode.
#
#  Valid options:
#
#     unspecified
#     centered
#     stretched
#
ScalingMode=unspecified

## Maximum allowed monitor refresh rate. Applies only in exclusive fullscreen mode.
#
#  This overrides Bethesda's hardcoded limitation of 60Hz. The game will request the highest
#  available refresh rate (as reported by DirectX), but there is no guarantee you'll actually get it.
#
MaximumRefreshRate=300

## General framerate limit. Applies everywhere, except where more specific limits are set (see below).
#
#  Warning: This plugin replaces the built-in limiter by default, 'bLockFramerate' will have no effect.
#
#  Bethesda default: 60
#
FramerateLimit=300

## Determines if the limiter is placed before or after frame presentation.
#
#  0 - before (favor consistent frametimes)
#  1 - after  (favor input latency)
#
FramerateLimitMode=1

## General UI framerate limit. Applies everywhere in paused menus, except where more specific limits are set.
#
#  Setting this or more specific options to -1 disables the respective limit explicitly.
#
UIFramerateLimit=0
UIFramerateLimitVSyncOff=true

## Framerate limit for the map. Useful if you want to adjust the map movement speed.
#
UIFramerateLimitMap=0
UIFramerateLimitMapVSyncOff=false

## Framerate limit for the inventory, magic, gift, barter, container and favorites menus.
#
UIFramerateLimitInventory=0
UIFramerateLimitInventoryVSyncOff=true

## Framerate limit for the journal.
#
UIFramerateLimitJournal=0
UIFramerateLimitJournalVSyncOff=true

## Framerate limit for custom menus.
#
UIFramerateLimitCustom=0
UIFramerateLimitCustomVSyncOff=true

## Framerate limit for the main menu.
#
#  It's recommended to keep this at 60 and UIFramerateLimitMainVSyncOff to false.
#
UIFramerateLimitMain=0
UIFramerateLimitMainVSyncOff=true

## Framerate limit for the race menu.
#
UIFramerateLimitRace=0
UIFramerateLimitRaceVSyncOff=true

## Framerate limit for the perk tree.
#
UIFramerateLimitPerk=0
UIFramerateLimitPerkVSyncOff=true

## Framerate limit while reading books.
#
UIFramerateLimitBook=0
UIFramerateLimitBookVSyncOff=true

## Framerate limit while picking locks.
#
UIFramerateLimitLockpick=0
UIFramerateLimitLockpickVSyncOff=true

## Framerate limit for the console.
#
UIFramerateLimitConsole=0
UIFramerateLimitConsoleVSyncOff=true

## Framerate limit for the tween menu.
#
UIFramerateLimitTween=0
UIFramerateLimitTweenVSyncOff=true

## Framerate limit for the sleep/wait menu.
#
UIFramerateLimitSleepWait=0
UIFramerateLimitSleepWaitVSyncOff=true

## Framerate limit while loading the game.
#
#  It's recommended you leave this at 60 and LoadingScreenFramerateLimitVSyncOff=false.
#  If you do choose to uncap, do not set a limit above 120 FPS.
#
LoadingScreenFramerateLimit=60
LoadingScreenFramerateLimitVSyncOff=false

## Additional time to keep the loading screen limit active (in seconds).
#
LoadingScreenLimitExtraTimePostLoad=2
LoadingScreenLimitExtraTime=2


[HAVOK]

## Master switch.
#
#  Set to false to fall back to Skyrim.ini HAVOK settings.
#
Enabled=true

## Adjusts fMaxTime and fMaxTimeComplex dynamically based on current framerate.
#
#  It's recommended to leave this enabled.
#
DynamicMaxTimeScaling=true

## fMaxTime and fMaxTimeComplex are not calculated below this threshold.
#
#  Default: 60
#
MinimumFramerate=60

## fMaxTime and fMaxTimeComplex are not calculated above this threshold.
#
#  Set to 0 to let the plugin determine the value automatically based on VSync and FramerateLimit.
#
MaximumFramerate=300

## Adjust the negative offset of fMaxTimeComplex relative to fMaxTime.
#
#  Valid range: 0 - 30
#
MaxTimeComplexOffset=30

## Adjust amount of damage dealt when hit by physics objects based on frametime.
#
#  Reduce or disable damage dealt with PhysicsDamageMult (valid range: 0 - 1).
#
PhysicsDamagePatch=true
PhysicsDamageMult=1

## Lowers HAVOK engine CPU consumption at the expense of physics simulation quality.
#
#  Only recommended on CPU-bound low-end systems.
#
PerformanceMode=false

## Add fMaxTime and fMaxTimeComplex to OSD.
#
OSDStatsEnabled=false


[Controls]

## Fixes an issue where you're not able to move in third person at high framerates.
#
ThirdPersonMovementFix=true

## Controls an internal variable related to movement/damping.
#
#  Valid range: 0.01 - 5
#
#  Bethesda default: 5
#
MovementThreshold=0.25

## Untie first person horizontal look sensitivity from framerate when sitting down
#
SittingHorizontalLookSensitivityFix=true

## Untie map keyboard movement speed from framerate.
#
MapMoveKeyboardSpeedFix=true

## Speed multiplier for keyboard map movement.
#
#  Valid range: -20 - 20.
#
MapMoveKeyboardSpeedMult=1

## Untie auto vanity camera rotation speed from framerate.
#
AutoVanityCameraSpeedFix=true

## Untie dialogue look speed from framerate.
#
DialogueLookSpeedFix=true

## Ramp up look speed incrementally as the cursor approaches the edge of the screen.
#
DialogueLookSmoothEdge=false

## Untie gamepad cursor speed from framerate.
#
GamepadCursorSpeedFix=true

## Untie lockpick mouse rotation speed from framerate.
#
LockpickRotationSpeedFix=true

## Untie free camera vertical sensitivity from framerate.
#
FreeCameraVerticalSensitivityFix=true

## Untie free camera movement speed from framerate.
#
FreeCameraMovementSpeedFix=true


## These are only available on AE until Engine Fixes is updated.

## Untie vertical look sensitivity from framerate (EngineFixes)
#
VerticalLookSensitivityFix=false

## Fix camera movement sensitivity during slow time (EngineFixes)
#
SlowTimeCameraMovementFix=false


[Window]

## Locks mouse cursor within the borders of the game window.
#
LockCursor=true

## Minimize the game window if it loses focus.
#
#  WARNING: Avoid using this with ENB or ReShade, things might break when alt-tabbing.
#
ForceMinimize=false

## Disables the window ghosting feature.
#
DisableProcessWindowsGhosting=true

## Automatically center the game window on the monitor where it spawns.
#
AutoCenter=false

## Offset the game window position relative to the primary monitor.
#
OffsetX=0
OffsetY=0


[Papyrus]

## Set the maximum time scripts are allowed to run per cycle based on current framerate.
#
DynamicUpdateBudget=false

## Amount of time scripts are alloted at or below 60 FPS (in milliseconds).
#
#  Bethesda default: 1.2
#
UpdateBudgetBase=1.2

## Budget is not calculated beyond this limit.
#
#  Valid range: 60 - 300
#
BudgetMaxFPS=300

## Fixes a bad range check in Actor.SetExpressionOverride papyrus function.
#
SetExpressionOverridePatch=true

## Add fUpdateBudgetMS to OSD.
#
OSDStatsEnabled=false

## Add VM overstressed indicator to OSD.
#
OSDWarnVMOverstressed=true

[Miscellaneous]

## Disables scanning missing plugin .ini files. May significantly improve startup times with many plugins.
#
SkipMissingPluginINI=true

## Filter loadscreens by plugin name.
#
#  LoadScreenAllow and LoadScreenBlock take comma separated plugin names (case-insensitive).
#
LoadScreenFilter=false
LoadScreenAllow=
LoadScreenBlock=All

## Remove all lens flare from weather records.
#
DisableWeatherLensFlare=false

## Disable actor fade when camera intersects the body.
#
DisableActorFade=false

## Disable player fade when camera intersects the body.
#
DisablePlayerFade=false

[OSD]

## Enable the on-screen display.
#
Enable=false
InitiallyOn=true

## Comma separated list of displayed stats.
#
#    fps                - Framerate
#    bare_fps           - Just the framerate, no formatting
#    frametime          - Frametime
#    bare_frametime     - Just the frametime, no formatting
#    counter            - Frame counter
#    vram               - Video ram usage (used / budget)
#    all                - Everything
#
Show=fps,vram

## How often the OSD updates (in seconds).
#
UpdateInterval=0.3

## Keys used used to toggle the OSD.
#
#  ComboKey:
#    1 - Left Shift
#    2 - Right Shift
#    3 - Left Control
#    4 - Right Control
#    5 - Left Alt
#    6 - Right Alt
#    7 - Left Win
#    8 - Right Win
#
#  ComboKey=1 and ToggleKey=0xD2 is Left Shift + Insert
#
ComboKey=1
ToggleKey=0xD2

## Align the OSD.
#
#    1 - Top Left
#    2 - Top Right
#    3 - Bottom Left
#    4 - Bottom Right
#
Align=1

## OSD position offset (X Y).
#
Offset=4 4

## Font scale (X Y)
#
#  Omit Y for uniform scaling
#
Scale=1.0 0.9

## Adjust font scale based on amount of lines drawn.
#
AutoScale=true

## Scale font size based on window size.
#
ScaleToWindow=true

## Set a custom font.
#
#  Run MakeSpriteFont with /NoPremultiply and place files in Data\\SKSE\\Plugins\\SDTFonts
#
FontFile=

## Font and outline color (RGBA).
#
Color=255 255 255 255
OutlineColor=0 0 0 255

## Outline offset.
#
OutlineOffset=1
"""


# ---------------------------------------------------------------------------
# Value loading / saving
# ---------------------------------------------------------------------------

def _schema_defaults() -> dict[tuple[str, str], tuple[str, bool]]:
    return {s.id: (s.default, s.enabled_by_default) for s in SCHEMA}


def is_installed(game: "BaseGame") -> bool:
    """True when SSEDisplayTweaks.dll is the winning file in the filemap."""
    from Utils.wizard_gates import filemap_find
    return filemap_find(game, REL_DLL_PATH) is not None


def load_initial_values(
    game: "BaseGame",
) -> tuple[dict[tuple[str, str], tuple[str, bool]], str]:
    """Resolve the initial form values and a one-line source description.

    Order: managed-mod ini -> filemap winner -> schema defaults. Loaded values
    overlay the schema defaults so every schema key still gets a row.
    """
    from Utils.wizard_gates import filemap_find

    values = _schema_defaults()
    source = "built-in defaults"

    managed = game.get_effective_mod_staging_path() / MOD_NAME / REL_INI_PATH
    src_path: "Path | None" = None
    if managed.is_file():
        src_path = managed
        source = f"managed mod '{MOD_NAME}'"
    else:
        fm = filemap_find(game, REL_INI_PATH)
        if fm is not None:
            src_path = fm
            source = "the deployed (filemap) ini"

    if src_path is not None:
        try:
            parsed = parse_ini(src_path.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            parsed = {}
        for ident, val in parsed.items():
            values[ident] = val
    return values, source


def schema_defaults() -> dict[tuple[str, str], tuple[str, bool]]:
    """Public alias for the built-in default value map."""
    return _schema_defaults()


def save_config(game: "BaseGame",
                values: dict[tuple[str, str], tuple[str, bool]]) -> Path:
    """Render *values* into the managed mod's ini (seeded from the existing
    ini or the documented default template) and write it atomically. Returns
    the written path; raises OSError on failure."""
    target = game.get_effective_mod_staging_path() / MOD_NAME / REL_INI_PATH
    try:
        base_text = (target.read_text(encoding="utf-8", errors="replace")
                     if target.is_file() else DEFAULT_INI)
    except OSError:
        base_text = DEFAULT_INI
    out = render_ini(base_text, values)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".mm_tmp")
    tmp.write_text(out, encoding="utf-8")
    tmp.replace(target)
    return target
