#!/bin/bash
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
# clients have no global coordinate system — window position reports as (0,0)
# and mapToGlobal is wrong — so QToolTip (which needs global coords to place the
# tip) mis-anchors, badly once QT_SCALE_FACTOR != 1 compounds the logical/
# physical size mismatch. XWayland exposes real global coords so tooltips place
# correctly and scaling stays exact; it also fixes the Wayland splitter/colour-
# picker lag. The flatpak build already does this. `:=` respects a user override.
: "${QT_QPA_PLATFORM:=xcb}"
export QT_QPA_PLATFORM

# The Qt app uses the PROJECT-ROOT .venv (../.venv), which has PySide6 —
# separate from src/.venv (the Tk app's venv, no PySide6) that run.sh uses.
VENV="../.venv"

if [ ! -d "$VENV" ]; then
    python3 -m venv "$VENV"
    [ -f requirements.txt ] && "$VENV/bin/pip" install -r requirements.txt -q
fi

# Install any missing or newly added requirements (incl. PySide6 for the Qt UI).
if [ -f requirements.txt ]; then
    "$VENV/bin/pip" install -r requirements.txt -q --disable-pip-version-check
fi
"$VENV/bin/python3" -c "import PySide6" 2>/dev/null || \
    "$VENV/bin/pip" install -q PySide6

# Tee stderr to a log so a native crash trace (faulthandler) and the bash
# "Segmentation fault" line survive after the terminal closes. Still shown live.
# One log per run (previous kept as .old) — appending forever mixes tracebacks
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
