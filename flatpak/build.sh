#!/bin/bash
# Build Amethyst Mod Manager as a Flatpak
#
# Prerequisites:
#   - Flatpak and flatpak-builder installed
#   - Freedesktop runtime: flatpak install flathub org.freedesktop.Platform//24.08 org.freedesktop.Sdk//24.08
#
# Usage:
#   ./flatpak/build.sh          # Build and install locally
#   ./flatpak/build.sh --export # Build only (no install)
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
MANIFEST="${SCRIPT_DIR}/io.github.Amethyst.ModManager.yml"
BUILD_DIR="${SCRIPT_DIR}/build"
INSTALL_FLAG="--install"
[ "${1:-}" = "--export" ] && INSTALL_FLAG=""

cd "$PROJECT_DIR"

echo "=== Building Amethyst Mod Manager Flatpak ==="
echo "  Manifest: $MANIFEST"
echo "  Project:  $PROJECT_DIR"
echo ""

flatpak-builder \
  --verbose \
  --user \
  --install-deps-from=flathub \
  $INSTALL_FLAG \
  "${BUILD_DIR}" \
  "${MANIFEST}"

if [ "${1:-}" != "--export" ]; then
  echo ""
  echo "=== Build and install complete ==="
  echo "Run with: flatpak run io.github.Amethyst.ModManager"
else
  echo ""
  echo "=== Build complete ==="
  echo "Build directory: ${BUILD_DIR}"
fi
