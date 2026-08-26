#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPOSITORY_DIR="$(cd -- "${SCRIPT_DIR}/../.." && pwd)"
MANIFEST_PATH="${SCRIPT_DIR}/Cargo.toml"
TARGET_DIR="${SCRIPT_DIR}/target"
BUILT_EXTENSION="${TARGET_DIR}/release/libamethyst_filegraph.so"
OUTPUT_EXTENSION="${REPOSITORY_DIR}/src/amethyst_filegraph.abi3.so"
FLATPAK_SDK="org.kde.Sdk//6.9"

if ! command -v cargo >/dev/null 2>&1; then
    echo "error: cargo is required to build amethyst_filegraph" >&2
    exit 1
fi

echo "Building amethyst_filegraph (release)..."
if command -v cc >/dev/null 2>&1; then
    CARGO_TARGET_DIR="${TARGET_DIR}" cargo build \
        --manifest-path "${MANIFEST_PATH}" \
        --release \
        --locked
else
    if ! command -v flatpak >/dev/null 2>&1 \
            || ! flatpak info "${FLATPAK_SDK}" >/dev/null 2>&1; then
        echo "error: no host C compiler or ${FLATPAK_SDK} was found" >&2
        echo "Install a C toolchain providing 'cc', or install the KDE SDK:" >&2
        echo "  flatpak install flathub ${FLATPAK_SDK}" >&2
        exit 1
    fi
    if ! command -v rustup >/dev/null 2>&1; then
        echo "error: the KDE SDK fallback requires a rustup-managed Cargo toolchain" >&2
        exit 1
    fi

    CARGO_BIN="$(command -v cargo)"
    CARGO_HOME_DIR="${CARGO_HOME:-$(cd -- "$(dirname -- "${CARGO_BIN}")/.." && pwd)}"
    RUSTUP_HOME_DIR="${RUSTUP_HOME:-$(rustup show home)}"
    echo "Host compiler not found; building with ${FLATPAK_SDK}..."
    flatpak run \
        --command=sh \
        --share=network \
        --filesystem="${REPOSITORY_DIR}" \
        --filesystem="${CARGO_HOME_DIR}" \
        --filesystem="${RUSTUP_HOME_DIR}:ro" \
        "${FLATPAK_SDK}" \
        -c 'CARGO_HOME="$4" RUSTUP_HOME="$5" CARGO_TARGET_DIR="$1" \
            "$2" build --manifest-path "$3" --release --locked' \
        amethyst-filegraph \
        "${TARGET_DIR}" \
        "${CARGO_BIN}" \
        "${MANIFEST_PATH}" \
        "${CARGO_HOME_DIR}" \
        "${RUSTUP_HOME_DIR}"
fi

if [[ ! -f "${BUILT_EXTENSION}" ]]; then
    echo "error: cargo did not produce ${BUILT_EXTENSION}" >&2
    exit 1
fi

mkdir -p -- "$(dirname -- "${OUTPUT_EXTENSION}")"
cp -f -- "${BUILT_EXTENSION}" "${OUTPUT_EXTENSION}"
echo "Installed ${OUTPUT_EXTENSION}"
