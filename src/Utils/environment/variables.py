"""User-configured environment variables for Amethyst itself.

Settings ▸ Advanced ▸ "Edit environment variables…" lets a user pin env vars
that are applied to the app's OWN process at startup - the kill switches and
diagnostics that were previously only reachable by launching from a terminal
with ``AMM_FOO=1 ./Amethyst.AppImage``. This is NOT the per-exe launch options
used for games/tools (those live in Settings ▸ Executables and executable launch settings).

`apply_saved_env` runs from ``app_bootstrap.setup_environment()``, before any
Qt / Utils import, so Qt-level vars (QT_QPA_PLATFORM, QT_XCB_GL_INTEGRATION)
are in place before the QApplication reads them.

Removal across a re-exec: Settings restarts the app with ``os.execv``, which
inherits the current environment - so simply not re-applying a var the user
deleted would leave the OLD value live. `_MARKER_VAR` records the names we set
last launch; anything listed there but no longer configured is unset.

Escape hatch: ``AMM_NO_ENV_OVERRIDES=1`` in the real shell skips the whole
thing, so a bad value can't lock a user out of the UI that fixes it.
"""

from __future__ import annotations

import os
import re

# Names we set on the previous launch, comma-separated. Lets us unset a var the
# user has since removed (see module docstring).
_MARKER_VAR = "_AMM_ENV_KEYS"

# Skips applying anything at all - the "I broke it from Settings" escape hatch.
_KILL_SWITCH = "AMM_NO_ENV_OVERRIDES"

_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# Vars that CANNOT work when set from inside an already-running interpreter, or
# that would break the app's own config/bundle resolution. Rejected in the
# editor with an explanation rather than silently ignored.
_BLOCKED_EXACT = {
    # The dynamic loader read these at exec; setting them now does nothing here
    # and leaks into every child process we spawn.
    "LD_PRELOAD", "LD_LIBRARY_PATH", "LD_AUDIT", "LD_DEBUG", "LD_BIND_NOW",
    # The interpreter is already up; sys.path is fixed by app_bootstrap.
    "PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP", "PYTHONEXECUTABLE",
    # Bundle identity - set by the AppImage runtime / flatpak, and used to find
    # the vendored libs and the Games/ dir. Overriding breaks the running app.
    "APPDIR", "APPIMAGE", "APPIMAGE_EXTRACT_AND_RUN", "FLATPAK_ID",
    "MOD_MANAGER_GAMES",
    # Would relocate the config dir - including the file this setting lives in.
    "HOME", "XDG_CONFIG_HOME",
    # Our own escape hatch: it must stay controllable from the real shell.
    _KILL_SWITCH,
}

# Internal markers (_AMM_OWNS_SCALE, _AMM_GL_PROBE_SYSPATH, _AMM_ENV_KEYS) are
# state we own, never user input.
_BLOCKED_PREFIXES = ("_AMM_",)


# ---------------------------------------------------------------------------
# Catalogue of variables the app itself understands.
#
# Kept in Utils (untranslated - these are technical option names, and Utils has
# no Qt to translate with). `values` non-empty → the editor offers a dropdown;
# None → a free-text value box.
# ---------------------------------------------------------------------------
_ONOFF = ["1", "0"]

KNOWN_VARS: list[dict] = [
    # --- graphics / display -------------------------------------------------
    {
        "name": "AMM_DISABLE_GL",
        "group": "Graphics",
        "values": _ONOFF,
        "default": "1",
        "summary": (
            "Force the app to treat OpenGL as unavailable. The 3D mesh viewer "
            "and other GL previews are hidden instead of rendered. Set this if "
            "the window comes up black - a broken GL driver blanks the whole "
            "window, not just the viewport."),
    },
    {
        "name": "AMM_FORCE_GL",
        "group": "Graphics",
        "values": _ONOFF,
        "default": "1",
        "summary": (
            "Trust this machine's OpenGL over the startup probe. The probe is "
            "deliberately conservative and can refuse a GL stack that actually "
            "works, which costs you the 3D preview - this overrules it."),
    },
    {
        "name": "QT_XCB_GL_INTEGRATION",
        "group": "Graphics",
        "values": ["xcb_glx", "xcb_egl"],
        "default": "xcb_egl",
        "summary": (
            "Which GL backend Qt uses on X11. Default is xcb_glx. Switch to "
            "xcb_egl if dragging a splitter next to the 3D viewer freezes the "
            "app; on some Xwayland setups xcb_egl renders nothing at all, so "
            "change it back if the window goes black."),
    },
    {
        "name": "QT_QPA_PLATFORM",
        "group": "Graphics",
        "values": ["xcb", "wayland"],
        "default": "xcb",
        "summary": (
            "Qt's windowing backend. Amethyst runs under xcb (XWayland) by "
            "default because native Wayland made splitter drags and window "
            "resizes lag badly. Only switch to wayland if you know your setup "
            "handles it."),
    },
    # --- deploy ------------------------------------------------------------
    {
        "name": "AMM_DEPLOY_INCREMENTAL",
        "group": "Deploy",
        "values": ["0", "1"],
        "default": "0",
        "summary": (
            "Kill switch for incremental deploys. 0 forces a full deploy every "
            "time - slower, but the fallback if a deploy ever misses a file."),
    },
    {
        "name": "AMM_DEPLOY_VERIFY",
        "group": "Deploy",
        "values": _ONOFF,
        "default": "1",
        "summary": (
            "Re-check every deployed file after an incremental deploy and log "
            "any divergence. Slower; use it to confirm a suspected bug."),
    },
    {
        "name": "MOD_MANAGER_DEPLOY_WORKERS",
        "group": "Deploy",
        "values": ["1", "4", "8", "16", "32"],
        "default": "16",
        "summary": (
            "Number of worker threads for parallel file transfers during "
            "deployment. Default is 16; values below 1 are treated as 1."),
    },
    # --- UI fast paths ------------------------------------------------------
    {
        "name": "AMM_TOGGLE_LIGHT",
        "group": "Interface",
        "values": ["0", "1"],
        "default": "0",
        "summary": (
            "Kill switch for the fast mod enable/disable path. 0 does a full "
            "conflict rebuild on every toggle - much slower on big load orders."),
    },
    {
        "name": "AMM_MOVE_SKIP_REBUILD",
        "group": "Interface",
        "values": ["0", "1"],
        "default": "0",
        "summary": (
            "Kill switch for the fast mod-reorder path. 0 forces a full "
            "conflict rebuild after every move."),
    },
    {
        "name": "AMM_PRUNE_OWNED",
        "group": "Interface",
        "values": ["0", "1"],
        "default": "0",
        "summary": (
            "Kill switch for pruning plugins owned by a disabled mod from "
            "plugins.txt. 0 leaves them listed, keeping their load-order slot."),
    },
    {
        "name": "AMETHYST_NO_SPLASH",
        "group": "Interface",
        "values": _ONOFF,
        "default": "1",
        "summary": "Skip the startup splash window.",
    },
    {
        "name": "AMM_SCANLINES",
        "group": "Interface",
        "values": ["0", "0.5", "1", "2"],
        "default": "0",
        "summary": (
            "Opacity multiplier for themes that use scanlines. 0 disables "
            "the effect, 1 uses the theme's normal intensity, and larger "
            "values make it stronger."),
    },
    # --- behaviour ----------------------------------------------------------
    {
        "name": "AMM_NO_SLEEP_INHIBIT",
        "group": "Behaviour",
        "values": _ONOFF,
        "default": "1",
        "summary": (
            "Don't hold off system sleep while a long tool run (a wizard, a "
            "deploy) is in progress."),
    },
    {
        "name": "AMM_FILE_REQS",
        "group": "Behaviour",
        "values": ["0", "1"],
        "default": "0",
        "summary": (
            "Kill switch for fetching a mod's file requirements from Nexus. 0 "
            "skips the extra API calls and the Requirements tab stays empty."),
    },
    {
        "name": "AMM_FLATPAK_OVERRIDE",
        "group": "Behaviour",
        "values": ["0", "1"],
        "default": "0",
        "summary": (
            "Kill switch for automatically granting the Flatpak sandbox access "
            "to game/staging folders outside home. 0 means you grant them "
            "yourself with flatpak override / Flatseal."),
    },
    {
        "name": "MOD_MANAGER_PROFILES_DIR",
        "group": "Behaviour",
        "values": None,
        "default": "",
        "summary": (
            "Override the default storage root for games using legacy profile "
            "storage. Use an absolute directory path; empty keeps automatic "
            "resolution. Does not move existing files or override a game's "
            "custom staging path."),
    },
    {
        "name": "AMM_BG3_PAK_UUID_CONFLICTS",
        "group": "Games",
        "values": ["0", "1"],
        "default": "0",
        "summary": (
            "Disable Baldur's Gate 3 conflict detection for PAKs that declare "
            "the same module UUID. Enabled by default; 0 disables these "
            "identity conflicts while retaining file-path conflicts."),
    },
    {
        "name": "AMM_CP2077_ARCHIVE_MODLIST",
        "group": "Games",
        "values": ["0", "1"],
        "default": "0",
        "summary": (
            "Disable Cyberpunk 2077 archive load-order management during "
            "deployment. 0 skips it; 1 still requires the game's Manage "
            "archive load order setting to be enabled."),
    },
    {
        "name": "AMM_DFU_MODS_JSON",
        "group": "Games",
        "values": ["0", "1"],
        "default": "0",
        "summary": (
            "Disable Daggerfall Unity Mods.json synchronization. 0 leaves "
            "the load order to the game's Mod Loader; 1 restores automatic "
            "synchronization."),
    },
    {
        "name": "AMM_FNV_AUTO_4GB",
        "group": "Games",
        "values": ["0", "1"],
        "default": "0",
        "summary": (
            "Disable Fallout New Vegas automatic 4 GB executable patching. "
            "0 skips it; 1 still respects the game's automatic patch setting."),
    },
    {
        "name": "AMM_STEAM_RUNTIME",
        "group": "Launchers",
        "values": ["0", "1"],
        "default": "0",
        "summary": (
            "Disable the Steam Linux Runtime wrapper for Proton game launches "
            "using Steam-managed prefixes. 0 launches without the wrapper; "
            "tool launches and non-Steam prefixes use their existing routes."),
    },
    {
        "name": "AMM_EPIC_AUTH",
        "group": "Launchers",
        "values": ["0", "1"],
        "default": "0",
        "summary": (
            "Disable fetching Epic authentication arguments through Heroic's "
            "Legendary client. 0 skips the request; games that require Epic "
            "login arguments may need to be launched through Heroic."),
    },
    {
        "name": "AMM_CPAK_OVERRIDE",
        "group": "Launchers",
        "values": ["0", "1"],
        "default": "0",
        "summary": (
            "Disable automatic filesystem permissions for Bottles installed "
            "through Cpak. With 0, grant access to the required game and "
            "staging directories yourself."),
    },
    {
        "name": "CPAK_INSTALLATION_PATH",
        "group": "Launchers",
        "values": None,
        "default": os.environ.get("CPAK_INSTALLATION_PATH") or os.path.expanduser("~/.local/share/cpak"),
        "summary": (
            "Cpak installation directory used to locate the Bottles CLI. "
            "Use an absolute path. The normal location is ~/.local/share/cpak; "
            "CPAK_EXPORTS_PATH can override its exports directory separately."),
    },
    {
        "name": "CPAK_EXPORTS_PATH",
        "group": "Launchers",
        "values": None,
        "default": os.environ.get("CPAK_EXPORTS_PATH") or os.path.join(
            os.environ.get("CPAK_INSTALLATION_PATH") or os.path.expanduser("~/.local/share/cpak"), "exports"),
        "summary": (
            "Cpak exports directory used to locate the Bottles CLI. Use an "
            "absolute path. Normally this is the exports subdirectory of "
            "CPAK_INSTALLATION_PATH."),
    },
    {
        "name": "AMM_PLUGIN_DIAG",
        "group": "Diagnostics",
        "values": _ONOFF,
        "default": "1",
        "summary": (
            "Verbose plugin-panel logging - every stage of a plugin list "
            "rebuild. Turn on when asked for a log about a wrong plugin list."),
    },
    {
        "name": "MM_PERFTRACE",
        "group": "Diagnostics",
        "values": _ONOFF,
        "default": "1",
        "summary": (
            "Enable performance logging for startup, interface operations, "
            "installers and the native Filegraph engine."),
    },
    {
        "name": "MM_PERFTRACE_MS",
        "group": "Diagnostics",
        "values": ["0", "8", "16", "50", "100"],
        "default": "8",
        "summary": (
            "Minimum operation duration in milliseconds for performance log "
            "messages. Default is 8; 0 logs every measured operation. "
            "Requires MM_PERFTRACE=1."),
    },
    {
        "name": "MM_PERFTRACE_INSTALL_MS",
        "group": "Diagnostics",
        "values": ["250", "1000", "5000", "10000"],
        "default": "5000",
        "summary": (
            "Interval in milliseconds between installer performance reports. "
            "Default is 5000, with a minimum of 250. Requires MM_PERFTRACE=1."),
    },
    {
        "name": "MM_COL_TIMING",
        "group": "Diagnostics",
        "values": ["1"],
        "default": "1",
        "summary": (
            "Log when collection download workers wait on the installation "
            "queue. Any non-empty value, including 0, enables this. "
            "Uncheck or remove the variable to disable it."),
    },
    {
        "name": "MM_BODYSLIDE_GLLOG",
        "group": "Diagnostics",
        "values": ["1"],
        "default": "1",
        "summary": (
            "Enable Wine/OpenGL diagnostics when launching BodySlide or "
            "Outfit Studio from the wizard. Any non-empty value, including 0, "
            "enables this. Uncheck or remove the variable to disable it."),
    },
    {
        "name": "AMETHYST_FILEGRAPH_TRACE",
        "group": "Diagnostics",
        "values": ["1"],
        "default": "1",
        "summary": (
            "Enable verbose native Filegraph timing and cache diagnostics. "
            "Any value, including 0 or an empty value, enables this. "
            "Uncheck or remove the variable to disable it."),
    },
    {
        "name": "AMETHYST_FILEGRAPH_QUICK_CHECK",
        "group": "Diagnostics",
        "values": ["1"],
        "default": "1",
        "summary": (
            "Run SQLite quick_check when opening a Filegraph database. "
            "Any value, including 0 or an empty value, enables this. "
            "Uncheck or remove the variable to disable it."),
    },
    {
        "name": "PORTAL_DEBUG",
        "group": "Diagnostics",
        "values": ["0", "1"],
        "default": "0",
        "summary": (
            "Control desktop portal file/folder chooser diagnostics. Enabled "
            "by default; 0 disables the detailed portal logging."),
    },
    # --- development --------------------------------------------------------
    {
        "name": "AMM_DEV_MODE",
        "group": "Development",
        "values": _ONOFF,
        "default": "1",
        "summary": (
            "Developer mode. Enables detailed Wabbajack, Nexus API and metadata "
            "logging. Stops the startup sync from overwriting your "
            "local custom handlers, wizard plugins and translations with the "
            "copies published on GitHub. Same switch as [dev] devmode in "
            "amethyst.ini, and it wins over it."),
    },
    {
        "name": "AMM_FORCE_MANUAL_INSTALL",
        "group": "Development",
        "values": _ONOFF,
        "default": "1",
        "summary": (
            "Always use the non-premium browser-download install flow, even "
            "with a Nexus premium account. For testing what non-premium users "
            "get. Same switch as [dev] force_manual_install in amethyst.ini."),
    },
    {
        "name": "AMM_WABBAJACK_SKIP_GAME_FILES",
        "group": "Development",
        "values": _ONOFF,
        "default": "1",
        "summary": (
            "Inspect Wabbajack file placement without the original game files. "
            "Skips game-file and vanilla-plugin checks, game-based setup and "
            "launch runtime requirements. Game-sourced outputs and files that "
            "depend on them are omitted. Mod files use the normal installation "
            "pipeline, and downloads are retained. The resulting installation "
            "is incomplete."),
    },
    {
        "name": "AMETHYST_FILEGRAPH_EXTENSION",
        "group": "Development",
        "values": None,
        "default": "",
        "summary": (
            "Load a development Filegraph shared library from an absolute "
            "file path. It must match Amethyst's current native API and schema. "
            "Leave empty to use the normal bundled or source-tree library."),
    },
]

KNOWN_BY_NAME = {v["name"]: v for v in KNOWN_VARS}


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------
def validate_name(name: str) -> str:
    """Return "" if *name* is a usable variable name, else why it isn't."""
    name = (name or "").strip()
    if not name:
        return "Enter a variable name."
    if not _NAME_RE.match(name):
        return ("Use letters, digits and underscores only, not starting with a "
                "digit.")
    if name in _BLOCKED_EXACT or name.startswith(_BLOCKED_PREFIXES):
        return ("{0} can't be set from here - it is read before the app starts "
                "or is managed by Amethyst itself.").format(name)
    return ""


def validate_value(value: str) -> str:
    """Return "" if *value* is storable in the environment, else why it isn't."""
    if "\0" in (value or ""):
        return "Values can't contain a null byte."
    return ""


def is_blocked(name: str) -> bool:
    return name in _BLOCKED_EXACT or name.startswith(_BLOCKED_PREFIXES)


# ---------------------------------------------------------------------------
# Apply
# ---------------------------------------------------------------------------
def apply_saved_env(environ: "dict[str, str] | None" = None,
                    log_fn=None) -> list[str]:
    """Apply the saved variables to *environ* (default ``os.environ``).

    Returns the names actually applied. Never raises - a failure here must not
    stop the app from starting.
    """
    env = os.environ if environ is None else environ

    def log(message: str) -> None:
        try:
            if log_fn is not None:
                log_fn(message)
        except Exception:
            pass
    try:
        previous = [n for n in env.get(_MARKER_VAR, "").split(",") if n]
        if env.get(_KILL_SWITCH, "").strip().lower() in ("1", "true", "yes", "on"):
            # Still undo last launch's vars, so the kill switch really is a
            # clean launch rather than "whatever the re-exec inherited".
            for name in previous:
                env.pop(name, None)
            env.pop(_MARKER_VAR, None)
            cleared = ", ".join(sorted(previous)) or "none"
            log(f"Startup environment: overrides disabled by {_KILL_SWITCH}; "
                f"cleared previous names: {cleared}.")
            return []

        from Utils.ui.config import load_app_env_vars
        entries = load_app_env_vars()

        applied: list[str] = []
        disabled: list[str] = []
        for entry in entries:
            name = entry.get("name", "")
            if not entry.get("enabled", True):
                if name:
                    disabled.append(name)
                continue
            invalid = validate_name(name) or validate_value(entry.get("value", ""))
            if invalid:
                log(f"Startup environment: skipped {name or '<blank name>'}: "
                    f"{invalid}")
                continue
            env[name] = str(entry.get("value", ""))
            applied.append(name)

        # Drop anything we set last launch that isn't configured any more - the
        # re-exec inherited it, so not re-applying it is not enough.
        removed: list[str] = []
        for name in previous:
            if name not in applied:
                env.pop(name, None)
                removed.append(name)

        if applied:
            env[_MARKER_VAR] = ",".join(applied)
        else:
            env.pop(_MARKER_VAR, None)
        log("Startup environment: applied names: "
            + (", ".join(sorted(applied)) if applied else "none") + ".")
        if disabled:
            log("Startup environment: disabled names: "
                + ", ".join(sorted(disabled)) + ".")
        if removed:
            log("Startup environment: removed inherited names: "
                + ", ".join(sorted(removed)) + ".")
        return applied
    except Exception as exc:
        log(f"Startup environment: could not load saved overrides: "
            f"{type(exc).__name__}: {exc}")
        return []
