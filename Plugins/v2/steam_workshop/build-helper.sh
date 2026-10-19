#!/usr/bin/env bash
set -euo pipefail
plugin_dir="$(cd -- "$(dirname -- "$0")" && pwd)"
case "${1:-$(uname -m)}" in
    x86_64|linux-x64) runtime=linux-x64 ;;
    aarch64|arm64|linux-arm64) runtime=linux-arm64 ;;
    *) echo 'Supported targets: linux-x64, linux-arm64' >&2; exit 1 ;;
esac
publish_dir="$(mktemp -d)"
trap 'rm -rf -- "$publish_dir"' EXIT
"${DOTNET:-dotnet}" publish "$plugin_dir/native/Amethyst.Workshop.csproj" \
    -c Release -r "$runtime" --self-contained true -o "$publish_dir" --nologo
install -Dm755 "$publish_dir/Amethyst.Workshop" "$plugin_dir/bin/$runtime/Amethyst.Workshop"
