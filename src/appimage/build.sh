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
echo "=== Installing pip dependencies ==="
PYTHON_BIN="${APPDIR}/opt/python3.13/bin/python3.13"
"$PYTHON_BIN" -m pip install --no-cache-dir --quiet \
    -r "${PROJECT_DIR}/requirements.txt"

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

# ── Step 5: Copy application code ────────────────────────────────────
echo "=== Copying application code ==="
APP_DIR="${APPDIR}/usr/app"
mkdir -p "$APP_DIR"

cp "${PROJECT_DIR}/gui.py" "$APP_DIR/"
cp "${PROJECT_DIR}/version.py" "$APP_DIR/"

for dir in gui Utils Games LOOT Nexus icons wizards wrappers; do
    cp -r "${PROJECT_DIR}/${dir}" "$APP_DIR/${dir}"
done

# Remove __pycache__ directories
find "$APP_DIR" -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true

# ── Step 6: Verify LOOT native library ───────────────────────────────
echo "=== Verifying LOOT native library ==="
LOOT_SO="$APP_DIR/LOOT/lib/loot/loot.cpython-313-x86_64-linux-gnu.so"
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
