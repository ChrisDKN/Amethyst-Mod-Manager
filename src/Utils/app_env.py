"""User-configured environment variables for Amethyst itself.

Settings ▸ Advanced ▸ "Edit environment variables…" lets a user pin env vars
that are applied to the app's OWN process at startup - the kill switches and
diagnostics that were previously only reachable by launching from a terminal
with ``AMM_FOO=1 ./Amethyst.AppImage``. This is NOT the per-exe launch options
used for games/tools (those live in Settings ▸ Executables → exe_launch.py).

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
    # --- deploy / filemap ---------------------------------------------------
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
        "name": "AMM_FILEMAP_INCREMENTAL",
        "group": "Deploy",
        "values": ["0", "1"],
        "default": "0",
        "summary": (
            "Kill switch for incremental filemap rebuilds. 0 rebuilds the whole "
            "conflict map on every change - slower, but immune to a stale map."),
    },
    {
        "name": "AMM_FILEMAP_VERIFY",
        "group": "Deploy",
        "values": _ONOFF,
        "default": "1",
        "summary": (
            "Build the filemap both incrementally and fully, and log any "
            "difference. Pair with a suspected wrong-winner conflict report."),
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
        "name": "AMM_PLUGIN_DIAG",
        "group": "Diagnostics",
        "values": _ONOFF,
        "default": "1",
        "summary": (
            "Verbose plugin-panel logging - every stage of a plugin list "
            "rebuild. Turn on when asked for a log about a wrong plugin list."),
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
def apply_saved_env(environ: "dict[str, str] | None" = None) -> list[str]:
    """Apply the saved variables to *environ* (default ``os.environ``).

    Returns the names actually applied. Never raises - a failure here must not
    stop the app from starting.
    """
    env = os.environ if environ is None else environ
    try:
        previous = [n for n in env.get(_MARKER_VAR, "").split(",") if n]
        if env.get(_KILL_SWITCH, "").strip().lower() in ("1", "true", "yes", "on"):
            # Still undo last launch's vars, so the kill switch really is a
            # clean launch rather than "whatever the re-exec inherited".
            for name in previous:
                env.pop(name, None)
            env.pop(_MARKER_VAR, None)
            return []

        from Utils.ui_config import load_app_env_vars
        entries = load_app_env_vars()

        applied: list[str] = []
        for entry in entries:
            name = entry.get("name", "")
            if not entry.get("enabled", True):
                continue
            if validate_name(name) or validate_value(entry.get("value", "")):
                continue
            env[name] = str(entry.get("value", ""))
            applied.append(name)

        # Drop anything we set last launch that isn't configured any more - the
        # re-exec inherited it, so not re-applying it is not enough.
        for name in previous:
            if name not in applied:
                env.pop(name, None)

        if applied:
            env[_MARKER_VAR] = ",".join(applied)
        else:
            env.pop(_MARKER_VAR, None)
        return applied
    except Exception:
        return []
