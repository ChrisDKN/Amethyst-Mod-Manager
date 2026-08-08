#!/bin/bash
# Build Amethyst Mod Manager as a Flatpak
#
# Prerequisites:
#   - Flatpak installed. flatpak-builder is provided by the org.flatpak.Builder
#     flatpak (installed automatically when missing - no sudo or rootfs writes).
#     Useful on SteamOS where the rootfs is read-only.
#   - KDE runtime + PySide BaseApp (auto-installed by --install-deps-from=flathub):
#       flatpak install flathub org.kde.Platform//6.9 org.kde.Sdk//6.9 io.qt.PySide.BaseApp//6.9
#   - 32-bit compat extensions (auto-installed by --install-deps-from=flathub):
#       org.freedesktop.Platform.Compat.i386//24.08
#       org.freedesktop.Platform.GL32//1.4
#     These provide /lib/i386-linux-gnu/ld-linux.so.2 etc., needed to exec
#     Proton's bundled 32-bit `wine` binary during Synthesis prefix setup.
#
# Usage:
#   ./flatpak/build.sh           # Build and install locally
#   ./flatpak/build.sh --export  # Build only (no install)
#   ./flatpak/build.sh --bundle  # Build and create .flatpak bundle file
#
set -euo pipefail

FB_FLATPAK_ID="org.flatpak.Builder"

# Returns the command to invoke flatpak-builder, preferring the flathub
# `org.flatpak.Builder` flatpak so we never touch the system package manager.
# Installs the flatpak on first use if missing.
resolve_flatpak_builder() {
  if flatpak info --user "$FB_FLATPAK_ID" >/dev/null 2>&1 \
     || flatpak info --system "$FB_FLATPAK_ID" >/dev/null 2>&1; then
    echo "flatpak run $FB_FLATPAK_ID"
    return 0
  fi
  if command -v flatpak-builder >/dev/null 2>&1; then
    echo "flatpak-builder"
    return 0
  fi
  echo "flatpak-builder not found; installing $FB_FLATPAK_ID from Flathub (--user, no sudo)..." >&2
  flatpak install --user -y --noninteractive flathub "$FB_FLATPAK_ID" >&2
  echo "flatpak run $FB_FLATPAK_ID"
}

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
MANIFEST="${SCRIPT_DIR}/io.github.Amethyst.ModManager.yml"
BUILD_DIR="${SCRIPT_DIR}/build"
REPO_DIR="${SCRIPT_DIR}/repo"
BUNDLE_FILE="${PROJECT_DIR}/AmethystModManager.flatpak"
APP_ID="io.github.Amethyst.ModManager"

INSTALL_FLAG="--install"
[ "${1:-}" = "--export" ] && INSTALL_FLAG=""
BUNDLE_MODE=false
[ "${1:-}" = "--bundle" ] && { INSTALL_FLAG=""; BUNDLE_MODE=true; }

cd "$PROJECT_DIR"

echo "=== Building Amethyst Mod Manager Flatpak ==="
echo "  Manifest: $MANIFEST"
echo "  Project:  $PROJECT_DIR"
echo ""

FB_CMD="$(resolve_flatpak_builder)"

$FB_CMD \
  --verbose \
  --user \
  --force-clean \
  --install-deps-from=flathub \
  --repo="${REPO_DIR}" \
  $INSTALL_FLAG \
  "${BUILD_DIR}" \
  "${MANIFEST}"

if [ "$BUNDLE_MODE" = true ]; then
  echo ""
  echo "=== Creating .flatpak bundle ==="
  flatpak build-bundle \
    "${REPO_DIR}" \
    "${BUNDLE_FILE}" \
    "${APP_ID}" \
    --runtime-repo=https://dl.flathub.org/repo/flathub.flatpakrepo
  echo ""
  echo "=== Bundle created: ${BUNDLE_FILE} ==="
  echo "Install with: flatpak install --user ${BUNDLE_FILE}"
  # Bundle installs do NOT pull the app's related refs - the 32-bit compat
  # extensions the manifest declares must be installed separately (the app
  # also self-heals this at startup via Utils/flatpak_i386.py).
  echo "Then install 32-bit support (bundle installs skip related refs):"
  echo "  flatpak install --user flathub org.freedesktop.Platform.Compat.i386//24.08 org.freedesktop.Platform.GL32.default//24.08"
elif [ "${1:-}" != "--export" ]; then
  echo ""
  echo "=== Build and install complete ==="
  echo "Run with: flatpak run ${APP_ID}"
else
  echo ""
  echo "=== Build complete ==="
  echo "Build directory: ${BUILD_DIR}"
fi
