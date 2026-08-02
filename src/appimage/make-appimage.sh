#!/bin/bash
# Build the Amethyst Mod Manager AppImage.
#
# CI-only build path: build a real Arch package via makepkg, install it to
# the host's /usr, then run quick-sharun. quick-sharun's `/usr → "$APPDIR"`
# path-rewriting catches hardcoded paths inside vendored deps that an
# AppDir-staged build would miss.
#
# Runs in the ghcr.io/pkgforge-dev/archlinux container (see
# .github/workflows/build.yml). For local testing, run the manual
# "Test Build" workflow on GitHub and download the artifact instead —
# there is no local staging mode anymore.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"   # = src/
SRC_TREE="$(dirname "$PROJECT_DIR")"     # = repo root

export PATH="$HOME/.local/bin:$PATH"

ARCH=$(uname -m)
VERSION=$(sed -n 's/^__version__ = "\(.*\)"$/\1/p' "${PROJECT_DIR}/version.py")
[ -n "$VERSION" ] || { echo "ERROR: cannot read __version__" >&2; exit 1; }

WORK_DIR="${TMPDIR:-/tmp}/amethyst-mm-build"
OUTPATH="${WORK_DIR}/dist"
APPDIR="${WORK_DIR}/AppDir"
FINAL_OUTPATH="${SCRIPT_DIR}/dist"

# ── Tooling check ────────────────────────────────────────────────────
for tool in quick-sharun cc awk find ldd strings wget makepkg pacman; do
    command -v "$tool" >/dev/null || {
        echo "ERROR: '$tool' not found in PATH" >&2
        exit 1
    }
done

if ! /usr/bin/python3 -c 'import PySide6.QtCore' 2>/dev/null; then
    echo "ERROR: /usr/bin/python3 cannot import PySide6." >&2
    echo "Install the system 'pyside6' package (Arch: pacman -S pyside6)." >&2
    exit 1
fi

# ── Clean ────────────────────────────────────────────────────────────
echo "=== Cleaning previous build ==="
rm -rf "$WORK_DIR" "$FINAL_OUTPATH"
mkdir -p "$APPDIR/bin" "$OUTPATH" "$FINAL_OUTPATH"

# ── Aux staging (bsdtar) ─────────────────────────────────────────────
# 7zzs and zenity-rs are bundled by the PKGBUILD itself. Only bsdtar lives
# here: it would conflict with the libarchive package.
AUX_DIR="${WORK_DIR}/aux"
mkdir -p "$AUX_DIR/bin"

echo "=== Bundling bsdtar ==="
BSDTAR_BIN="$(command -v bsdtar 2>/dev/null || true)"
if [ -n "$BSDTAR_BIN" ]; then
    cp "$BSDTAR_BIN" "$AUX_DIR/bin/bsdtar"
    chmod +x "$AUX_DIR/bin/bsdtar"
fi

# Desktop / icon — quick-sharun reads these via env vars.
ASSETS_DIR="${WORK_DIR}/assets"
mkdir -p "$ASSETS_DIR"
cp "${SCRIPT_DIR}/mod-manager.desktop" "$ASSETS_DIR/mod-manager.desktop"
cp "${SCRIPT_DIR}/mod-manager.png"     "$ASSETS_DIR/mod-manager.png"

# ── Locate the libloot extension (built by the separate CI job or by
# running src/LOOT/rebuild_libloot.sh locally — either way it lands at
# the src/ root, where `import loot` finds it).
LIBLOOT_SO="$(find "${PROJECT_DIR}" -maxdepth 1 -name 'loot.cpython-*-x86_64-linux-gnu.so' 2>/dev/null | head -1 || true)"
if [ -z "$LIBLOOT_SO" ]; then
    echo "WARN: no libloot .so found in src/ — the AppImage will lack LOOT support" >&2
fi

# ── quick-sharun env ─────────────────────────────────────────────────
# DEPLOY_PYTHON=1   pulls /usr/bin/python3 + stdlib + site-packages (PySide6)
# DEPLOY_QT=1       Qt plugin deployment. NOTE: quick-sharun's auto-detection
#                   does NOT fire for PySide6 — it dlopens Qt at runtime so
#                   libQt6Core never enters the wrapper's ldd trace. We force
#                   it here AND hand quick-sharun the Qt libs/plugins directly
#                   (see the resolution block before the quick-sharun call).
# ALWAYS_SOFTWARE=1 forces software rendering (matches upstream)
# ANYLINUX_LIB=1    builds anylinux.so (LD_PRELOAD env-scrubber for child procs)
export ARCH VERSION OUTPATH APPDIR
export ICON="${ASSETS_DIR}/mod-manager.png"
export DESKTOP="${ASSETS_DIR}/mod-manager.desktop"
export DEPLOY_PYTHON=1
export DEPLOY_QT=1
export ALWAYS_SOFTWARE=1
export ANYLINUX_LIB=1

# SteamOS strips glibc headers from /usr/include; quick-sharun's anylinux.so
# build needs dlfcn.h. Sysroot at ~/sdk/include works around that.
if [ -f "$HOME/sdk/include/dlfcn.h" ]; then
    export C_INCLUDE_PATH="$HOME/sdk/include${C_INCLUDE_PATH:+:$C_INCLUDE_PATH}"
elif [ ! -f /usr/include/dlfcn.h ]; then
    echo "  NOTE: ~/sdk/include not found and /usr/include/dlfcn.h missing — disabling anylinux.so." >&2
    export ANYLINUX_LIB=0
fi

# ── Build + install the package ──────────────────────────────────────
echo "=== Building amethyst-mod-manager package via makepkg ==="
PKG_BUILD_DIR="${WORK_DIR}/pkgbuild"
mkdir -p "$PKG_BUILD_DIR"
cp "${SCRIPT_DIR}/PKGBUILD" "$PKG_BUILD_DIR/PKGBUILD"

# makepkg refuses to run as root. If we are root, drop to a non-root
# user. The CI workflow creates a 'builder' user with passwordless sudo;
# locally, the script is presumably already non-root.
_makepkg_uid=""
if [ "$(id -u)" = "0" ]; then
    if id builder >/dev/null 2>&1; then
        _makepkg_uid=builder
    else
        echo "ERROR: makepkg cannot run as root; create a 'builder' user first" >&2
        exit 1
    fi
    # The build user needs read access to both the PKGBUILD scratch dir
    # and the source tree (PKGBUILD reads $SRC_TREE/src/version.py and
    # package() copies from there).
    chown -R "$_makepkg_uid":"$_makepkg_uid" "$PKG_BUILD_DIR"
    chmod -R a+rX "$SRC_TREE"
fi

# Pass SRC_TREE / LIBLOOT_SO through the env so the PKGBUILD picks them up.
_libloot_arg=""
[ -n "$LIBLOOT_SO" ] && _libloot_arg="LIBLOOT_SO=$LIBLOOT_SO"
if [ -n "$_makepkg_uid" ]; then
    sudo -u "$_makepkg_uid" \
         env SRC_TREE="$SRC_TREE" $_libloot_arg \
         bash -c "cd '$PKG_BUILD_DIR' && makepkg --noconfirm --nodeps"
else
    ( cd "$PKG_BUILD_DIR" && SRC_TREE="$SRC_TREE" ${_libloot_arg:+env $_libloot_arg} makepkg --noconfirm --nodeps )
fi

PKG_FILE=$(find "$PKG_BUILD_DIR" -maxdepth 1 -name 'amethyst-mod-manager-*.pkg.tar.*' -type f | head -1)
[ -n "$PKG_FILE" ] || { echo "ERROR: makepkg produced no package" >&2; exit 1; }

echo "=== Installing $PKG_FILE ==="
# --overwrite for re-runs that hit the same version; --nodeps because
# we vendor everything pip-installed and depend only on python/pyside6
# which are already present in the container.
pacman -U --noconfirm --overwrite '*' --nodeps "$PKG_FILE"

# ── Resolve the system Qt6 libs for PySide6 ──────────────────────────
# PySide6 dlopens Qt at runtime from its own extension modules, so
# quick-sharun's ldd trace of the (shell → python3) wrapper never sees
# libQt6Gui.so and skips its Qt-plugin deployment entirely — the AppImage
# then dies with "Could not find the Qt platform plugin 'xcb'".
#
# The fix is just to get the Qt LIBS into the arg list: once libQt6Gui.so
# is traced, quick-sharun's own handler globs and deploys the plugin dirs
# (platforms/imageformats/styles/wayland/xcbglintegrations), and libQt6-
# Network.so pulls tls/. We do NOT pass the individual plugin .so files —
# that just doubled the (expensive) per-file ldd work in quick-sharun's
# dependency-collection pass, making the build take many extra minutes.
#
# Arch's pyside6 depends on qt6-base and uses the SYSTEM Qt at
# /usr/lib/libQt6*.so + /usr/lib/qt6/plugins/, so those paths are stable.
QT_PLUGIN_DIR=""
for _d in /usr/lib/qt6/plugins /usr/lib/qt/plugins; do
    [ -d "$_d/platforms" ] && { QT_PLUGIN_DIR="$_d"; break; }
done
[ -n "$QT_PLUGIN_DIR" ] || { echo "ERROR: qt6 platform plugins not found (is qt6-base installed?)" >&2; exit 1; }

# Remove the GTK theme-integration plugin so quick-sharun's platform*/*
# glob can't pick it up: it links libgtk-3 (which we don't bundle), and
# tracing it makes quick-sharun abort with "missing libraries". We keep
# libqxdgdesktopportal.so (portal-based theme, no GTK dep). The Qt app
# applies its own Breeze/QSS theme, so it needs neither.
rm -f "$QT_PLUGIN_DIR/platformthemes/libqgtk3.so"

_qt_args=()
# Core Qt libs PySide6 needs — Gui triggers the plugin-deployment block,
# Network triggers tls/. XcbQpa (X11) and WaylandClient are the platform
# abstraction libs the platform plugins link; quick-sharun traces them
# transitively but listing them removes any doubt. OpenGL + OpenGLWidgets:
# QOpenGLWidget (nif viewer) lives in libQt6OpenGLWidgets — without it the
# loader falls back to the host's /usr/lib copy and dies on Qt_6_PRIVATE_API.
for _l in libQt6Core libQt6Gui libQt6Widgets libQt6DBus libQt6Network \
          libQt6XcbQpa libQt6WaylandClient libQt6OpenGL libQt6OpenGLWidgets; do
    for _so in /usr/lib/"$_l".so*; do
        [ -e "$_so" ] && _qt_args+=("$_so")
    done
done

# ── Resolve libshiboken6 / libpyside6 ────────────────────────────────
_pyside_dir="$(/usr/bin/python3 -c 'import PySide6, os; print(os.path.dirname(PySide6.__file__))')"
[ -n "$_pyside_dir" ] && [ -d "$_pyside_dir" ] || {
    echo "ERROR: could not locate the PySide6 package directory" >&2; exit 1; }
_site_dir="$(dirname "$_pyside_dir")"     # site-packages/ (holds shiboken6/)
_pyside_libs=()
_seen_names=""
for _root in \
    "$_pyside_dir" \
    "$_site_dir/shiboken6" \
    /usr/lib; do
    [ -d "$_root" ] || continue
    for _so in \
        "$_root"/libshiboken6.*.so* \
        "$_root"/libpyside6.*.so* \
        "$_root"/Shiboken.*.so; do
        [ -e "$_so" ] || continue
        # de-dup by basename (a lib may be reachable from >1 root)
        case " $_seen_names " in *" $(basename "$_so") "*) continue ;; esac
        _seen_names="$_seen_names $(basename "$_so")"
        _pyside_libs+=("$_so")
    done
done
# Both core libs are mandatory — fail loudly if either is missing rather
# than shipping an AppImage that ImportErrors on the user's machine.
case "$_seen_names" in *libshiboken6.*) : ;; *)
    echo "ERROR: libshiboken6 not found (searched $_pyside_dir, $_site_dir/shiboken6, /usr/lib)" >&2
    exit 1 ;; esac
case "$_seen_names" in *libpyside6.*) : ;; *)
    echo "ERROR: libpyside6 not found (searched $_pyside_dir, $_site_dir/shiboken6, /usr/lib)" >&2
    exit 1 ;; esac
echo "  PySide6 runtime libs deployed:"
printf '    %s\n' "${_pyside_libs[@]}"

echo "=== Running quick-sharun ==="
# Stdlib extension modules in lib-dynload are dlopened at runtime, so
# quick-sharun's per-binary ldd trace never sees their DT_NEEDED entries
# and silently drops the underlying libs. Each line below covers a stdlib
# ext we actually import (directly or transitively):
#   libssl/libcrypto -> _ssl.so      (HTTPS — Nexus, GitHub, updates)
#   libuuid          -> _uuid.so     (uuid.uuid4 used by Nexus SSO)
#   libmpdec         -> _decimal.so  (transitive deps may import decimal)
# The Qt libs/plugins in $_qt_args are dlopened by PySide6 for the same
# reason — see the resolution block above.
quick-sharun \
    /usr/bin/mod-manager               \
    /usr/share/amethyst-mod-manager    \
    /usr/bin/7zzs                      \
    /usr/bin/zenity                    \
    /usr/lib/libssl.so*                \
    /usr/lib/libcrypto.so*             \
    /usr/lib/libuuid.so*               \
    /usr/lib/libmpdec.so*              \
    "${_qt_args[@]}"                   \
    "${_pyside_libs[@]}"               \
    $( [ -f "$AUX_DIR/bin/bsdtar" ] && printf %s "$AUX_DIR/bin/bsdtar" )

# Rewrite the wrapper's /usr/share path to "$APPDIR"/share — quick-sharun's
# built-in /usr → "$APPDIR" rewrite only fires for dotnet scripts, so plain
# shell wrappers need this manual step.
sed -i -e 's|/usr/share|"$APPDIR"/share|g' "$APPDIR/bin/mod-manager"

# Strip __pycache__ from our app tree. The PKGBUILD's package() cleans these,
# but Arch's python ALPM hook re-generates .pyc files on `pacman -U`; quick-
# sharun's DEBLOAT_SYS_PYTHON only touches $APPDIR/shared/lib/python*. ~4M.
find "$APPDIR/share/amethyst-mod-manager" -type d -name '__pycache__' \
    -exec rm -rf {} + 2>/dev/null || true

# ── Hicolor icon for AppImageLauncher / appimaged integration ────────
# libappimage resolves Icon=mod-manager via the FreeDesktop spec, i.e.
# usr/share/icons/hicolor/<size>/apps/<name>.png. Without it AppImageLauncher
# logs "no icon to set" and refuses to write a host .desktop file.
# quick-sharun deploys binaries + libs but doesn't propagate icon themes,
# so we install the icon into the AppDir explicitly here.
install -Dm644 "${ASSETS_DIR}/mod-manager.png" \
    "$APPDIR/usr/share/icons/hicolor/256x256/apps/mod-manager.png"

# ── Build the AppImage ───────────────────────────────────────────────
echo "=== Building AppImage ==="
# OUTNAME: appimagetool reads this from the env — naming the output here
# (instead of renaming afterwards) keeps the generated .zsync consistent
# with the final filename.
# UPINFO: embedded update info + .zsync generation. Set explicitly so the
# zsync glob matches OUTNAME; without this appimagetool guesses the same
# thing from GITHUB_REPOSITORY, but the docs say not to rely on the guess.
_gh_repo="${GITHUB_REPOSITORY:-ChrisDKN/Amethyst-Mod-Manager}"
export OUTNAME="AmethystModManager-${VERSION}-${ARCH}.AppImage"
export UPINFO="gh-releases-zsync|${_gh_repo%/*}|${_gh_repo#*/}|latest|*${ARCH}.AppImage.zsync"
quick-sharun --make-appimage

FINAL="${FINAL_OUTPATH}/${OUTNAME}"
if [ -f "$OUTPATH/$OUTNAME" ]; then
    mv "$OUTPATH/$OUTNAME" "$FINAL"
fi
# The .zsync must be published next to the AppImage for the embedded
# update info to work (AppImageUpdate / Gear Lever / appimaged).
for _zs in "$OUTPATH"/*.zsync; do
    [ -e "$_zs" ] && mv "$_zs" "$FINAL_OUTPATH/"
done

echo ""
echo "=== Build complete ==="
[ -f "$FINAL" ] && {
    echo "AppImage: $FINAL"
    echo "Size: $(du -h "$FINAL" | cut -f1)"
}
