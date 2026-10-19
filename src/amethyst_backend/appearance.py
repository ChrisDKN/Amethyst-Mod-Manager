from __future__ import annotations

from contextlib import redirect_stdout
import sys


def list_themes():
    from themes import load_display_names, load_palettes
    with redirect_stdout(sys.stderr):
        palettes = load_palettes()
        names = load_display_names()
    return {"themes": sorted(
        ({"id": tid, "name": names.get(tid, tid), "custom": tid.startswith("custom:")}
         for tid in palettes), key=lambda theme: (theme["name"].casefold(), theme["id"]))}


def get_appearance():
    from Utils.ui.config import get_appearance_mode
    return {"theme_id": get_appearance_mode()}


def set_appearance(theme_id):
    from Utils.ui.config import save_appearance_mode
    if not isinstance(theme_id, str) or theme_id not in {
            theme["id"] for theme in list_themes()["themes"]}:
        raise ValueError("Select an available theme ID from appearance.themes.list.")
    save_appearance_mode(theme_id)
    return get_appearance()
