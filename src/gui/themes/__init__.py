"""
Theme palettes — one file per theme.

Adding a new theme:
    1. Copy an existing file (dark.py / light.py) to e.g. `solarized.py`.
    2. Edit the NAME constant and the PALETTE dict values.
    3. Keep every key present in the palette (missing keys = broken UI).
    4. The theme is auto-discovered at startup; no other code changes needed.

Each theme module must export:
    NAME:    str                              — human-readable label (shown in Settings dropdown)
    PALETTE: dict[str, str | tuple]           — every color constant the app uses
"""

from __future__ import annotations

import importlib
import pkgutil
from pathlib import Path
from typing import Iterator


def _iter_theme_modules() -> Iterator[str]:
    """Yield the module name of every .py file in this package except __init__."""
    pkg_dir = Path(__file__).parent
    for mod in pkgutil.iter_modules([str(pkg_dir)]):
        if not mod.ispkg and not mod.name.startswith("_"):
            yield mod.name


def load_palettes() -> dict[str, dict[str, str | tuple]]:
    """Discover and import every theme file; return {theme_id: palette_dict}.

    theme_id is the module filename without .py (e.g. 'dark', 'light').
    Malformed theme files (missing PALETTE / NAME, or import errors) are
    skipped with a warning rather than crashing the app.
    """
    palettes: dict[str, dict[str, str | tuple]] = {}
    for mod_name in _iter_theme_modules():
        try:
            mod = importlib.import_module(f"{__name__}.{mod_name}")
            palette = getattr(mod, "PALETTE", None)
            if isinstance(palette, dict):
                palettes[mod_name] = palette
            else:
                print(f"[themes] skipping {mod_name}: no PALETTE dict", flush=True)
        except Exception as exc:
            print(f"[themes] failed to load {mod_name}: {exc}", flush=True)
    return palettes


def load_display_names() -> dict[str, str]:
    """Return {theme_id: human-readable NAME} for every discovered theme."""
    names: dict[str, str] = {}
    for mod_name in _iter_theme_modules():
        try:
            mod = importlib.import_module(f"{__name__}.{mod_name}")
            names[mod_name] = getattr(mod, "NAME", mod_name.title())
        except Exception:
            pass
    return names


def get_ctk_appearance(theme_id: str) -> str:
    """Return the CTk appearance mode ("light" or "dark") the given theme
    declares via its CTK_APPEARANCE attribute. Defaults to "dark" if the
    theme is missing, the attribute is absent, or the value is invalid.
    """
    try:
        mod = importlib.import_module(f"{__name__}.{theme_id}")
        value = str(getattr(mod, "CTK_APPEARANCE", "dark")).strip().lower()
        return value if value in ("light", "dark") else "dark"
    except Exception:
        return "dark"
