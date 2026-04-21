#!/bin/bash
# Build Amethyst Mod Manager as a Flatpak
#
# Prerequisites:
#   - Flatpak installed; flatpak-builder is installed automatically when missing (needs sudo)
#   - GNOME runtime: flatpak install flathub org.gnome.Platform//49 org.gnome.Sdk//49
#   - 32-bit compat extensions (auto-installed by --install-deps-from=flathub):
#       org.freedesktop.Platform.Compat.i386//25.08
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

ensure_flatpak_builder() {
  if command -v flatpak-builder >/dev/null 2>&1; then
    return 0
  fi
  echo "flatpak-builder not found; installing via system package manager (sudo required)..."
  if command -v pacman >/dev/null 2>&1; then
    sudo pacman -S --needed --noconfirm flatpak-builder
  elif command -v apt-get >/dev/null 2>&1; then
    sudo apt-get update -qq
    sudo DEBIAN_FRONTEND=noninteractive apt-get install -y flatpak-builder
  elif command -v dnf >/dev/null 2>&1; then
    sudo dnf install -y flatpak-builder
  elif command -v zypper >/dev/null 2>&1; then
    sudo zypper install -y flatpak-builder
  elif command -v apk >/dev/null 2>&1; then
    sudo apk add flatpak-builder
  else
    echo "Could not detect a package manager. Install flatpak-builder manually, then re-run this script." >&2
    exit 1
  fi
  if ! command -v flatpak-builder >/dev/null 2>&1; then
    echo "flatpak-builder is still not available after install. Check the output above." >&2
    exit 1
  fi
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

ensure_flatpak_builder

flatpak-builder \
  --verbose \
  --user \
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
elif [ "${1:-}" != "--export" ]; then
  echo ""
  echo "=== Build and install complete ==="
  echo "Run with: flatpak run ${APP_ID}"
else
  echo ""
  echo "=== Build complete ==="
  echo "Build directory: ${BUILD_DIR}"
fi
