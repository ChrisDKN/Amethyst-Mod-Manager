#!/bin/bash
set -euo pipefail

# ──────────────────────────────────────────────────────────────────────
# Mod Manager — AppImage build script
#
# Usage:  bash appimage/build.sh
# Output: appimage/build/AmethystModManager-<version>-x86_64.AppImage
# ──────────────────────────────────────────────────────────────────────

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
BUILD_DIR="${SCRIPT_DIR}/build"
APPDIR="${BUILD_DIR}/AmethystModManager.AppDir"

# Read version from version.py for output filename
VERSION=$(sed -n 's/^__version__ = "\(.*\)"$/\1/p' "${PROJECT_DIR}/version.py")
if [ -z "$VERSION" ]; then
    echo "ERROR: Could not read __version__ from ${PROJECT_DIR}/version.py"
    exit 1
fi
OUTPUT_APPIMAGE="AmethystModManager-${VERSION}-x86_64.AppImage"

# URLs — update these if newer versions are available
PYTHON_APPIMAGE_URL="https://github.com/niess/python-appimage/releases/download/python3.13/python3.13.9-cp313-cp313-manylinux_2_28_x86_64.AppImage"
APPIMAGETOOL_URL="https://github.com/AppImage/appimagetool/releases/download/continuous/appimagetool-x86_64.AppImage"

# ── Clean previous build ─────────────────────────────────────────────
echo "=== Cleaning previous build ==="
rm -rf "$BUILD_DIR"
mkdir -p "$BUILD_DIR"

# ── Step 1: Download Python AppImage ─────────────────────────────────
PYTHON_APPIMAGE="${BUILD_DIR}/python3.13.AppImage"
echo "=== Downloading Python 3.13 AppImage ==="
wget -O "$PYTHON_APPIMAGE" "$PYTHON_APPIMAGE_URL"
chmod +x "$PYTHON_APPIMAGE"

# ── Step 2: Extract into AppDir ──────────────────────────────────────
echo "=== Extracting Python AppImage ==="
cd "$BUILD_DIR"
"$PYTHON_APPIMAGE" --appimage-extract >/dev/null 2>&1
mv squashfs-root "$APPDIR"

# ── Step 3: Install pip dependencies ─────────────────────────────────
# Use requirements-appimage.txt (excludes PyGObject — needs system GLib to build).
# Portal file chooser falls back to zenity when PyGObject is unavailable.
echo "=== Installing pip dependencies ==="
PYTHON_BIN="${APPDIR}/opt/python3.13/bin/python3.13"
"$PYTHON_BIN" -m pip install --no-cache-dir --quiet \
    -r "${PROJECT_DIR}/requirements-appimage.txt"

# ── Step 4: Bundle libarchive system library ─────────────────────────
# libarchive-c is a ctypes wrapper that needs the system libarchive.so
echo "=== Bundling libarchive ==="
LIBARCHIVE_SO="$(ldconfig -p 2>/dev/null | grep 'libarchive\.so\.' | head -1 | awk '{print $NF}')"
if [ -n "$LIBARCHIVE_SO" ]; then
    cp "$LIBARCHIVE_SO" "${APPDIR}/usr/lib/"
    echo "  Bundled: $LIBARCHIVE_SO"
else
    echo "  WARNING: libarchive.so not found on system — install libarchive-dev"
fi

# ── Step 4b: Bundle 7-Zip static binary ──────────────────────────────
# 7zzs is a fully static build from the official ip7z/7zip project.
# It handles BCJ2 and all modern 7z compression methods with no .so deps.
echo "=== Bundling 7-Zip (7zzs) ==="
SEVENZIP_URL="https://github.com/ip7z/7zip/releases/download/26.00/7z2600-linux-x64.tar.xz"
SEVENZIP_TAR="${BUILD_DIR}/7z-linux-x64.tar.xz"
SEVENZIP_TMP="${BUILD_DIR}/7z-tmp"
mkdir -p "${APPDIR}/usr/bin" "$SEVENZIP_TMP"
wget -q -O "$SEVENZIP_TAR" "$SEVENZIP_URL"
tar -xf "$SEVENZIP_TAR" -C "$SEVENZIP_TMP"
cp "$SEVENZIP_TMP/7zzs" "${APPDIR}/usr/bin/7zzs"
chmod +x "${APPDIR}/usr/bin/7zzs"
rm -rf "$SEVENZIP_TAR" "$SEVENZIP_TMP"
echo "  Bundled: 7zzs ($(du -h "${APPDIR}/usr/bin/7zzs" | cut -f1))"

# ── Step 4c: Create 7z/7za symlinks pointing to 7zzs ─────────────────
# Ensures shutil.which("7z") and shutil.which("7za") also resolve to
# the bundled static binary when the host has no 7zip package installed.
echo "=== Creating 7z/7za compatibility symlinks ==="
ln -sf 7zzs "${APPDIR}/usr/bin/7z"
ln -sf 7zzs "${APPDIR}/usr/bin/7za"
echo "  Symlinked: 7z → 7zzs, 7za → 7zzs"

# ── Step 4d: Bundle bsdtar ────────────────────────────────────────────
# bsdtar (libarchive CLI) is the final .7z fallback.  Copy the binary
# and all non-libc shared libraries so it works on any distro.
# libarchive.so is already in usr/lib; remaining deps (liblzma, libbz2,
# libzstd, liblz4, libxml2, libicu*, libacl, libcrypto, etc.) go there too.
echo "=== Bundling bsdtar ==="
BSDTAR_BIN="$(which bsdtar 2>/dev/null || true)"
if [ -n "$BSDTAR_BIN" ]; then
    cp "$BSDTAR_BIN" "${APPDIR}/usr/bin/bsdtar"
    chmod +x "${APPDIR}/usr/bin/bsdtar"
    # Copy every .so dependency except the handful that MUST come from the host
    ldd "$BSDTAR_BIN" 2>/dev/null | awk '/=>/ { print $3 }' \
        | grep -Ev '(linux-vdso|ld-linux|libc\.so|libm\.so|libdl\.so|libpthread\.so|librt\.so|libgcc_s\.so|libstdc\+\+\.so)' \
        | while read -r lib; do
            [ -f "$lib" ] || continue
            if cp -n "$lib" "${APPDIR}/usr/lib/" 2>/dev/null; then
                echo "  Bundled dep: $(basename "$lib")"
            fi
        done
    echo "  Bundled: bsdtar ($(du -h "${APPDIR}/usr/bin/bsdtar" | cut -f1))"
else
    echo "  WARNING: bsdtar not found on build system — skipping"
fi

# ── Step 4e: Bundle PyGObject (gi) for transparent GTK splash ────────
# python-gobject can't be pip-installed; we copy the compiled .so files and
# their GLib/GObject shared library deps from the build system instead.
# The splash script falls back gracefully if these are missing at runtime.
echo "=== Bundling PyGObject (gi) for transparent splash ==="
PYTHON_SITE="${APPDIR}/opt/python3.13/lib/python3.13/site-packages"
# Find the system gi package
GI_SYSTEM_PATH="$(python3 -c "import gi; import os; print(os.path.dirname(gi.__file__))" 2>/dev/null || true)"
if [ -n "$GI_SYSTEM_PATH" ] && [ -d "$GI_SYSTEM_PATH" ]; then
    cp -r "$GI_SYSTEM_PATH" "$PYTHON_SITE/gi"
    echo "  Copied gi from: $GI_SYSTEM_PATH"
    # Also copy cairo Python bindings if available
    CAIRO_SYSTEM_PATH="$(python3 -c "import site; import os; [print(os.path.join(p,'cairo')) for p in site.getsitepackages() if os.path.isdir(os.path.join(p,'cairo'))]" 2>/dev/null | head -1 || true)"
    if [ -n "$CAIRO_SYSTEM_PATH" ] && [ -d "$CAIRO_SYSTEM_PATH" ]; then
        cp -r "$CAIRO_SYSTEM_PATH" "$PYTHON_SITE/cairo"
        echo "  Copied cairo from: $CAIRO_SYSTEM_PATH"
    fi
    # Bundle the GLib/GObject/GIO/GTK shared libraries that gi needs
    for lib in libglib-2.0 libgobject-2.0 libgio-2.0 libgmodule-2.0 \
               libgdk_pixbuf-2.0 libgtk-3 libgdk-3 libatk-1.0 \
               libpango-1.0 libpangocairo-1.0 libpangoft2-1.0 \
               libcairo libcairo-gobject libcairo-script-interpreter \
               libffi libpcre2-8 libgthread-2.0 libharfbuzz libepoxy; do
        so="$(ldconfig -p 2>/dev/null | grep "^\s*${lib}\.so\." | head -1 | awk '{print $NF}' || true)"
        if [ -n "$so" ] && [ -f "$so" ]; then
            if cp -n "$so" "${APPDIR}/usr/lib/" 2>/dev/null; then
                echo "  Bundled: $(basename "$so")"
            fi
        fi
    done
    # Bundle GIR typelib files so gi.require_version works
    TYPELIB_DIRS="/usr/lib/x86_64-linux-gnu/girepository-1.0 /usr/lib/girepository-1.0"
    for tdir in $TYPELIB_DIRS; do
        if [ -d "$tdir" ]; then
            mkdir -p "${APPDIR}/usr/lib/girepository-1.0"
            for typelib in Gtk-3.0.typelib Gdk-3.0.typelib GLib-2.0.typelib \
                           GObject-2.0.typelib Gio-2.0.typelib GdkPixbuf-2.0.typelib \
                           Pango-1.0.typelib PangoCairo-1.0.typelib cairo-1.0.typelib; do
                [ -f "$tdir/$typelib" ] && cp -n "$tdir/$typelib" "${APPDIR}/usr/lib/girepository-1.0/" && \
                    echo "  Bundled typelib: $typelib"
            done
            break
        fi
    done
else
    echo "  WARNING: python3-gi not found on build system — GTK splash will be skipped at runtime"
fi

# ── Step 4f: Bundle Cantarell font ───────────────────────────────────
# Cantarell (OFL 1.1) is used for checkbox widgets — on hosts without it
# Tk silently substitutes another font and the checkboxes render poorly.
# We ship Regular + Bold which covers every `font=("Cantarell", …)` site.
echo "=== Bundling Cantarell font ==="
CANTARELL_URL="https://gitlab.gnome.org/GNOME/cantarell-fonts/-/archive/0.303.1/cantarell-fonts-0.303.1.tar.gz"
CANTARELL_TAR="${BUILD_DIR}/cantarell.tar.gz"
CANTARELL_TMP="${BUILD_DIR}/cantarell-tmp"
FONT_DEST="${APPDIR}/usr/share/fonts/amethyst"
mkdir -p "$FONT_DEST" "$CANTARELL_TMP"
if wget -q -O "$CANTARELL_TAR" "$CANTARELL_URL"; then
    tar -xf "$CANTARELL_TAR" -C "$CANTARELL_TMP"
    # Upstream ships a variable OTF (Cantarell-VF.otf) that covers all weights.
    VF_OTF="$(find "$CANTARELL_TMP" -name 'Cantarell-VF.otf' | head -1)"
    if [ -n "$VF_OTF" ]; then
        cp "$VF_OTF" "$FONT_DEST/"
        echo "  Bundled: Cantarell-VF.otf"
    else
        echo "  WARNING: Cantarell-VF.otf not found in archive"
    fi
    rm -rf "$CANTARELL_TAR" "$CANTARELL_TMP"
else
    echo "  WARNING: Could not download Cantarell — checkboxes may look wrong on hosts without it"
    rm -rf "$CANTARELL_TAR" "$CANTARELL_TMP"
fi

# ── Step 5: Copy application code ────────────────────────────────────
echo "=== Copying application code ==="
APP_DIR="${APPDIR}/usr/app"
mkdir -p "$APP_DIR"

cp "${PROJECT_DIR}/gui.py" "$APP_DIR/"
cp "${PROJECT_DIR}/cli.py" "$APP_DIR/"
cp "${PROJECT_DIR}/version.py" "$APP_DIR/"
cp "${PROJECT_DIR}/splash_gtk.py" "$APP_DIR/" 2>/dev/null || true
cp "${PROJECT_DIR}/../Changelog.txt" "$APP_DIR/" 2>/dev/null || true

for dir in gui Utils Games LOOT Nexus icons wizards wrappers; do
    cp -r "${PROJECT_DIR}/${dir}" "$APP_DIR/${dir}"
done

# Remove __pycache__ directories
find "$APP_DIR" -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true

# ── Step 6: Verify LOOT native library ───────────────────────────────
echo "=== Verifying LOOT native library ==="
LOOT_SO="$APP_DIR/LOOT/loot.cpython-313-x86_64-linux-gnu.so"
if [ -f "$LOOT_SO" ]; then
    echo "  Found: $(basename "$LOOT_SO") ($(du -h "$LOOT_SO" | cut -f1))"
else
    echo "  WARNING: LOOT native library not found at expected path"
fi

# ── Step 6b: Remove bundled X11 libs that conflict with the host ─────
# The python-appimage bundles old libXrender/libXft that cause BadLength
# errors with newer X servers. Let the system provide these instead.
echo "=== Removing bundled X11 libraries ==="
rm -f "$APPDIR"/usr/lib/libX*.so* "$APPDIR"/usr/lib/libxcb*.so*

# ── Step 6c: Remove bundled ncurses/tinfo ─────────────────────────────
# python-appimage ships libncursesw; AppRun puts usr/lib first on LD_LIBRARY_PATH.
# Host libreadline.so then loads that ncurses instead of the system's and the
# dynamic linker warns: "no version information available (required by libreadline)".
# Dropping the bundle matches host readline ↔ host ncurses (same as X11 fix above).
echo "=== Removing bundled ncurses/tinfo (readline uses host libs) ==="
rm -f "$APPDIR"/usr/lib/libncurses*.so* "$APPDIR"/usr/lib/libtinfo*.so* 2>/dev/null || true

# ── Step 7: Desktop integration ──────────────────────────────────────
echo "=== Setting up desktop integration ==="

# Remove Python AppImage's own desktop/appdata files so they don't conflict
rm -f "$APPDIR"/*.desktop
rm -f "$APPDIR"/*.png "$APPDIR"/*.svg
rm -rf "$APPDIR/usr/share/metainfo" "$APPDIR/usr/share/appdata"
rm -rf "$APPDIR/usr/share/applications"

cp "${SCRIPT_DIR}/mod-manager.desktop" "$APPDIR/"
cp "${SCRIPT_DIR}/mod-manager.png" "$APPDIR/"

mkdir -p "$APPDIR/usr/share/icons/hicolor/256x256/apps"
cp "${SCRIPT_DIR}/mod-manager.png" "$APPDIR/usr/share/icons/hicolor/256x256/apps/"

# ── Step 8: Install custom AppRun ────────────────────────────────────
echo "=== Installing AppRun ==="
rm -f "$APPDIR/AppRun"
cp "${SCRIPT_DIR}/AppRun" "$APPDIR/AppRun"
chmod +x "$APPDIR/AppRun"

# ── Step 9: Build the AppImage ───────────────────────────────────────
echo "=== Downloading appimagetool ==="
APPIMAGETOOL="${BUILD_DIR}/appimagetool"
wget -O "$APPIMAGETOOL" "$APPIMAGETOOL_URL"
chmod +x "$APPIMAGETOOL"

echo "=== Building AppImage ==="
cd "$BUILD_DIR"
ARCH=x86_64 "$APPIMAGETOOL" --no-appstream "$APPDIR" "$OUTPUT_APPIMAGE"

# ── Done ─────────────────────────────────────────────────────────────
echo ""
echo "=== Build complete ==="
echo "AppImage: ${BUILD_DIR}/${OUTPUT_APPIMAGE}"
echo "Size: $(du -h "${BUILD_DIR}/${OUTPUT_APPIMAGE}" | cut -f1)"
