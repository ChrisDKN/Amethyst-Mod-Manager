#!/usr/bin/env bash
# Rebuild the libloot Python extension (loot.cpython-313-x86_64-linux-gnu.so) from
# https://github.com/loot/libloot and place it so the Mod Manager and AppImage can use it.
#
# Usage:
#   ./LOOT/rebuild_libloot.sh              # clone/update and build latest master
#   ./LOOT/rebuild_libloot.sh v0.29.0      # build a specific release tag
#
# Requires: bash, git, Python 3.13, Rust (cargo), and a C toolchain (cc/gcc).
# The script creates/uses a .venv in the project root and installs requirements + maturin there.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
VENV_DIR="${PROJECT_DIR}/.venv"
LIBLOOT_DIR="${SCRIPT_DIR}/libloot"
PYTHON_DIR="${LIBLOOT_DIR}/python"
OUT_SO_NAME="loot.cpython-313-x86_64-linux-gnu.so"
OUT_PRIMARY="${SCRIPT_DIR}/${OUT_SO_NAME}"
OUT_APPDIR="${SCRIPT_DIR}/lib/loot/${OUT_SO_NAME}"
REQUIREMENTS="${PROJECT_DIR}/requirements.txt"

# Optional: build a specific tag or commit (e.g. v0.29.0)
REF="${1:-}"

echo "=== Rebuilding libloot Python extension ==="
echo "  Project root: $PROJECT_DIR"
echo "  venv: $VENV_DIR"
echo "  libloot clone: $LIBLOOT_DIR"
echo "  Output (primary): $OUT_PRIMARY"
echo "  Output (AppImage): $OUT_APPDIR"
echo ""

# ── Require C toolchain (Rust needs it to link) ───────────────────────
if ! command -v cc >/dev/null 2>&1 && ! command -v gcc >/dev/null 2>&1; then
    echo "ERROR: No C compiler (cc/gcc) found. Rust needs it to build the extension." >&2
    echo "Install a C toolchain, then run this script again. Examples:" >&2
    echo "  Debian/Ubuntu:  sudo apt install build-essential" >&2
    echo "  Arch:            sudo pacman -S base-devel" >&2
    echo "  Fedora:          sudo dnf install gcc" >&2
    echo "  openSUSE:        sudo zypper install gcc" >&2
    exit 1
fi

# ── Create .venv and install requirements + maturin ───────────────────
if [ ! -d "$VENV_DIR" ]; then
    echo "=== Creating .venv ==="
    python3 -m venv "$VENV_DIR"
fi
VENV_PYTHON="${VENV_DIR}/bin/python"
VENV_PIP="${VENV_DIR}/bin/pip"
echo "=== Ensuring .venv has requirements and maturin ==="
"$VENV_PIP" install -q -r "$REQUIREMENTS" maturin
echo ""

# ── Clone or update libloot ───────────────────────────────────────────
if [ ! -d "$LIBLOOT_DIR" ]; then
    echo "=== Cloning libloot ==="
    if [ -n "$REF" ]; then
        git clone --branch "$REF" --depth 1 https://github.com/loot/libloot.git "$LIBLOOT_DIR"
    else
        git clone --depth 1 https://github.com/loot/libloot.git "$LIBLOOT_DIR"
    fi
else
    echo "=== Updating libloot ==="
    (cd "$LIBLOOT_DIR" && git fetch origin && { [ -z "$REF" ] || git fetch origin "tag/${REF}" 2>/dev/null || true; } && git checkout "${REF:-origin/master}")
fi

if [ ! -d "$PYTHON_DIR" ]; then
    echo "ERROR: libloot python directory not found: $PYTHON_DIR" >&2
    exit 1
fi

# Embed revision into build (libloot's build.rs uses this)
export LIBLOOT_REVISION
LIBLOOT_REVISION="$(cd "$LIBLOOT_DIR" && git rev-parse --short HEAD 2>/dev/null || echo "unknown")"
echo "  LIBLOOT_REVISION=$LIBLOOT_REVISION"
echo ""

# ── Build the wheel ───────────────────────────────────────────────────
echo "=== Building libloot Python wheel (release) ==="
cd "$PYTHON_DIR"
"$VENV_PYTHON" -m maturin build --release --interpreter "$VENV_PYTHON"

# ── Locate the built wheel ────────────────────────────────────────────
# Maturin may put target/wheels under python/ or under the repo root
for WHEEL_DIR in "${PYTHON_DIR}/target/wheels" "${LIBLOOT_DIR}/target/wheels"; do
    WHEEL=( "$WHEEL_DIR"/libloot-*-cp313-*linux*.whl "$WHEEL_DIR"/loot-*-cp313-*linux*.whl )
    for w in "${WHEEL[@]}"; do
        if [ -f "$w" ]; then
            WHEEL="$w"
            break 2
        fi
    done
done
if [ -z "${WHEEL:-}" ] || [ ! -f "$WHEEL" ]; then
    echo "ERROR: No cp313 linux wheel found in target/wheels under libloot or libloot/python." >&2
    for d in "${PYTHON_DIR}/target/wheels" "${LIBLOOT_DIR}/target/wheels"; do
        [ -d "$d" ] && ls -la "$d" 2>/dev/null || true
    done
    exit 1
fi
echo "  Wheel: $WHEEL"
echo ""

# ── Extract .so from wheel and place it ────────────────────────────────
echo "=== Installing extension into LOOT ==="
TMP_EXTRACT="$(mktemp -d)"
trap 'rm -rf "$TMP_EXTRACT"' EXIT
unzip -q -o "$WHEEL" -d "$TMP_EXTRACT"

# Wheel may have .so at top level or under a package dir; module may be "loot" or "libloot"
SO_FILE=""
for candidate in "$TMP_EXTRACT/${OUT_SO_NAME}" \
                 "$TMP_EXTRACT/loot/${OUT_SO_NAME}" \
                 "$TMP_EXTRACT/libloot/${OUT_SO_NAME}"; do
    if [ -f "$candidate" ]; then
        SO_FILE="$candidate"
        break
    fi
done
if [ -z "$SO_FILE" ]; then
    SO_FILE="$(find "$TMP_EXTRACT" -name "*.so" -type f | head -1)"
fi
if [ -z "$SO_FILE" ] || [ ! -f "$SO_FILE" ]; then
    echo "ERROR: No .so found inside wheel. Contents:" >&2
    find "$TMP_EXTRACT" -type f
    exit 1
fi

mkdir -p "$(dirname "$OUT_APPDIR")"
cp -f "$SO_FILE" "$OUT_PRIMARY"
cp -f "$SO_FILE" "$OUT_APPDIR"
echo "  Installed: $OUT_PRIMARY"
echo "  Installed: $OUT_APPDIR"
echo ""
echo "=== Done. You can run the Mod Manager or build the AppImage."
