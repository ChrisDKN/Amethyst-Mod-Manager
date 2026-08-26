#!/bin/bash

# Let run_qt.py include shell/venv setup and Python interpreter startup in the
# same end-to-end timeline.  The Python entry point consumes this marker so a
# later self-reexec starts a fresh clock instead of inheriting this launch.
if [ -z "${_AMM_LAUNCH_WALL_STARTED:-}" ]; then
    if [ -n "${EPOCHREALTIME:-}" ]; then
        _AMM_LAUNCH_WALL_STARTED="$EPOCHREALTIME"
    else
        _AMM_LAUNCH_WALL_STARTED="$(date +%s.%N)"
    fi
    export _AMM_LAUNCH_WALL_STARTED
fi

cd "$(dirname "$0")" || exit 1

# Drop AppImage-injected env that poisons a from-source run. If the user
# launched a terminal from a running AppImage at any point, these inherit
# into the shell and point at /tmp/.mount_*; once the AppImage exits the
# mount goes away and Python startup fails with "Failed to import encodings"
# or "ImportError: cannot import name '_imaging' from 'PIL'".
unset PYTHONPATH PYTHONHOME APPDIR APPIMAGE OWD URUNTIME ARG0 ARGV0
unset SHARUN_DIR SHARUN_WORKING_DIR APPIMAGE_ARCH APPIMAGE_UUID
unset GIO_LAUNCH_DESKTOP GDK_PIXBUF_MODULEDIR GDK_PIXBUF_MODULE_FILE
unset GIO_MODULE_DIR GSETTINGS_SCHEMA_DIR GTK_PATH GTK_IM_MODULE_FILE
unset QT_PLUGIN_PATH TERMINFO LIBTHAI_DICTDIR
unset SSL_CERT_FILE SSL_CERT_DIR CURL_CA_BUNDLE LD_LIBRARY_PATH LD_PRELOAD
# Drop a stale MOD_MANAGER_GAMES pointing at a vanished mount so gui.py
# re-discovers the source tree's Games/ dir.
case "${MOD_MANAGER_GAMES:-}" in /tmp/.mount_*) unset MOD_MANAGER_GAMES ;; esac
# Strip /tmp/.mount_* fragments from PATH / XDG_DATA_DIRS so a stale
# AppImage's bin/ doesn't shadow system tools.
PATH=$(echo "$PATH" | tr ':' '\n' | grep -v '^/tmp/\.mount_' | paste -sd:)
[ -n "${XDG_DATA_DIRS:-}" ] && \
    XDG_DATA_DIRS=$(echo "$XDG_DATA_DIRS" | tr ':' '\n' | grep -v '^/tmp/\.mount_' | paste -sd:)
export PATH XDG_DATA_DIRS

# Force XWayland (xcb) rather than native Wayland. Under native Wayland, Qt
# clients have no global coordinate system - window position reports as (0,0)
# and mapToGlobal is wrong - so QToolTip (which needs global coords to place the
# tip) mis-anchors, badly once QT_SCALE_FACTOR != 1 compounds the logical/
# physical size mismatch. XWayland exposes real global coords so tooltips place
# correctly and scaling stays exact; it also fixes the Wayland splitter/colour-
# picker lag. The flatpak build already does this. A user override wins.
# The marker tells Utils.xdg.host_env() the value is OURS, so it is scrubbed
# from anything we launch - a child that can't reach an X server (the OpenMW
# flatpak has fallback-x11 only) aborts on startup if it inherits xcb.
if [ -z "${QT_QPA_PLATFORM:-}" ]; then
    QT_QPA_PLATFORM=xcb
    _AMM_OWNS_QT_PLATFORM=1
    export _AMM_OWNS_QT_PLATFORM
fi
export QT_QPA_PLATFORM

# The Qt app uses the PROJECT-ROOT .venv (../.venv), which has PySide6 -
# separate from src/.venv (the Tk app's venv, no PySide6) that run.sh uses.
VENV="../.venv"

if [ ! -d "$VENV" ]; then
    python3 -m venv "$VENV"
fi

# Dependency resolution used to run on every launch, followed by a second
# interpreter that imported all of PySide6 just to check it.  Cache a checksum
# of both requirement files instead: normal launches do no pip or extra Python
# work, while a checkout that changes dependencies still updates the venv once.
if [ -f requirements.txt ]; then
    _req_stamp="$VENV/.amethyst-requirements.cksum"
    _req_fingerprint="$(cksum requirements.txt requirements-vendor.txt 2>/dev/null)"
    _installed_fingerprint=""
    [ -f "$_req_stamp" ] && _installed_fingerprint="$(cat "$_req_stamp")"
    if [ "$_req_fingerprint" != "$_installed_fingerprint" ] || \
            ! compgen -G "$VENV/lib/python*/site-packages/PySide6/__init__.py" \
                >/dev/null; then
        if "$VENV/bin/pip" install -r requirements.txt -q \
                --disable-pip-version-check; then
            printf '%s\n' "$_req_fingerprint" > "$_req_stamp"
        else
            exit $?
        fi
    fi
fi

# Tee stderr to a log so a native crash trace (faulthandler) and the bash
# "Segmentation fault" line survive after the terminal closes. Still shown live.
# One log per run (previous kept as .old) - appending forever mixes tracebacks
# from old builds into current triage.
_errlog="${XDG_CONFIG_HOME:-$HOME/.config}/AmethystModManager/run-qt-stderr.log"
mkdir -p "$(dirname "$_errlog")"
[ -f "$_errlog" ] && mv -f "$_errlog" "$_errlog.old"
# Tell the app the launcher already tees stderr to a file, so the in-Python
# capture (app_bootstrap → install_stderr_file) stands down and doesn't redirect
# fd 2 out from under this tee. AppImage/flatpak don't run this script, so there
# the Python capture takes over and writes the same log.
export AMM_STDERR_TEED=1
"$VENV/bin/python3" run_qt.py "$@" 2> >(tee "$_errlog" >&2)
