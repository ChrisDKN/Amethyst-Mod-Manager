from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


def load_modio_module(stem: str):
    if stem not in {"modio_api", "modio_key", "modio_meta", "modio_update_checker"}:
        raise ValueError(f"Unknown BG3 mod.io module: {stem}")
    name = f"{stem}_bg3"
    cached = sys.modules.get(name)
    if cached is not None:
        return cached
    path = Path(__file__).resolve().parents[2] / "Games" / "Baldur's Gate 3" / f"{stem}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Could not load BG3 mod.io module: {stem}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(name, None)
        raise
    return module
