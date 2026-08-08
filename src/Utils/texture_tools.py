"""
GUI-neutral gates + preset table for the VRAMr / BENDr / ParallaxR wizards.

The wrappers (wrappers/vramr.py, bendr.py, parallaxr.py) are already neutral;
this module just holds the install-detection helpers and VRAMr's preset table
so the Qt wizard view can share them with the Tk wizards.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from Games.base_game import BaseGame

# (key, label, description) - VRAMr's optimisation presets.
VRAMR_PRESETS = [
    ("hq",          "High Quality",  "2K / 2K / 1K / 1K  - 4K modlist downscaled to 2K"),
    ("quality",     "Quality",       "2K / 1K / 1K / 1K  - Balance of quality & savings"),
    ("optimum",     "Optimum",       "2K / 1K / 512 / 512 - Good starting point"),
    ("performance", "Performance",   "2K / 512 / 512 / 512 - Big gains, lower close-up"),
    ("vanilla",     "Vanilla",       "512 / 512 / 512 / 512 - Just run the game"),
]


def applications_dir(game: "BaseGame", app_dir: str) -> Path:
    return game.get_mod_staging_path().parent / "Applications" / app_dir


def vramr_installed(game: "BaseGame") -> bool:
    app_dir = applications_dir(game, "VRAMr")
    return (app_dir / "VRAMr.exe").is_file() or (app_dir / "tools").is_dir()


def texture_tool_installed(game: "BaseGame", app_dir: str) -> bool:
    """BENDr / ParallaxR are 'installed' once their tools/ dir is present."""
    return (applications_dir(game, app_dir) / "tools").is_dir()
