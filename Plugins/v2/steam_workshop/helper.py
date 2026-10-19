import os
from pathlib import Path
import platform


def helper_path():
    target = {"x86_64": "linux-x64", "aarch64": "linux-arm64", "arm64": "linux-arm64"}.get(platform.machine())
    if platform.system() != "Linux" or target is None:
        raise RuntimeError("Workshop currently supports Linux x64 and ARM64.")
    path = Path(__file__).parent / "bin" / target / "Amethyst.Workshop"
    if not path.is_file() or not os.access(path, os.X_OK):
        raise RuntimeError(f"Workshop helper for {target} is missing. Run this plugin's build-helper.sh with .NET SDK 8 installed.")
    return path
