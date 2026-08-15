"""User-authored theme palettes stored as JSON.

Built-in themes are Python modules under ``Utils/themes/*.py``. Custom themes
authored via the Qt theme editor live as JSON files in a user-writable config
folder (``<config>/themes/``) so they survive app updates and can be shared as
single files.

JSON schema (one file per theme)::

    {
        "NAME": "My Theme",              # human-readable label (Settings dropdown)
        "CTK_APPEARANCE": "dark",        # "dark" or "light"
        "PALETTE": { "BG_DEEP": "#101010", ... }   # every palette key
    }

Theme ids are namespaced ``custom:<stem>`` (the filename without ``.json``) so
they never collide with the built-in ids (``dark``, ``light``, ...). The
loaders here are deliberately toolkit-free - they're merged into the neutral
``Utils.themes`` discovery layer used by both the Tk and Qt front-ends.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from Utils.config_paths import get_config_dir

# Prefix that marks a theme id as a user JSON theme (vs. a built-in .py module).
CUSTOM_PREFIX = "custom:"


def get_custom_themes_dir() -> Path:
    """Return ``<config>/themes/``, creating it if it doesn't exist."""
    d = get_config_dir() / "themes"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _slugify(name: str) -> str:
    """Turn a display name into a safe filename stem."""
    slug = re.sub(r"[^a-zA-Z0-9._-]+", "-", name.strip()).strip("-._")
    return slug.lower() or "theme"


def _stem_from_id(theme_id: str) -> str:
    """Strip the CUSTOM_PREFIX from a namespaced id to get the filename stem."""
    return theme_id[len(CUSTOM_PREFIX):] if theme_id.startswith(CUSTOM_PREFIX) else theme_id


def is_custom_theme(theme_id: str) -> bool:
    """True if *theme_id* refers to a user JSON theme (vs. a built-in)."""
    return bool(theme_id) and theme_id.startswith(CUSTOM_PREFIX)


def _path_for_stem(stem: str) -> Path:
    return get_custom_themes_dir() / f"{stem}.json"


def load_custom_palettes() -> dict[str, dict]:
    """Return ``{theme_id: palette_dict}`` for every JSON theme on disk.

    Malformed files (bad JSON, missing PALETTE) are skipped with a warning
    rather than crashing, mirroring the built-in theme loader.
    """
    out: dict[str, dict] = {}
    try:
        themes_dir = get_custom_themes_dir()
    except Exception:
        return out
    for path in sorted(themes_dir.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            palette = data.get("PALETTE")
            if isinstance(palette, dict) and palette:
                out[CUSTOM_PREFIX + path.stem] = palette
            else:
                print(f"[custom_themes] skipping {path.name}: no PALETTE dict",
                      flush=True)
        except Exception as exc:
            print(f"[custom_themes] failed to load {path.name}: {exc}", flush=True)
    return out


def load_custom_display_names() -> dict[str, str]:
    """Return ``{theme_id: NAME}`` for every JSON theme on disk."""
    out: dict[str, str] = {}
    try:
        themes_dir = get_custom_themes_dir()
    except Exception:
        return out
    for path in sorted(themes_dir.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            out[CUSTOM_PREFIX + path.stem] = str(data.get("NAME") or path.stem)
        except Exception:
            pass
    return out


def get_custom_ctk_appearance(theme_id: str) -> str | None:
    """Return the CTK_APPEARANCE ("dark"/"light") declared by a custom theme,
    or ``None`` if the theme isn't a custom JSON theme / can't be read."""
    if not is_custom_theme(theme_id):
        return None
    path = _path_for_stem(_stem_from_id(theme_id))
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        value = str(data.get("CTK_APPEARANCE", "dark")).strip().lower()
        return value if value in ("light", "dark") else "dark"
    except Exception:
        return "dark"


def save_custom_theme(name: str, palette: dict,
                      ctk_appearance: str = "dark",
                      base_palette: dict | None = None,
                      theme_id: str | None = None,
                      overwrite: bool = False) -> str:
    """Write a custom theme to ``<config>/themes/<stem>.json`` and return its id.

    ``palette`` may contain only the keys the user edited; when *base_palette*
    is given the two are merged so the saved file always carries a full key set
    (the app's "missing keys = broken UI" invariant). When *theme_id* is an
    existing custom id the same file is overwritten (an edit); otherwise a stem
    is derived from *name*. By default the stem is de-duplicated against existing
    files (Save As → a distinct new theme); pass ``overwrite=True`` to reuse the
    stem verbatim when a caller intentionally updates an existing name.
    """
    full: dict = dict(base_palette or {})
    full.update(palette or {})

    if theme_id and is_custom_theme(theme_id):
        stem = _stem_from_id(theme_id)
    else:
        stem = _slugify(name)
        # De-duplicate (my-theme, my-theme-2, ...) unless overwriting in place.
        if not overwrite and _path_for_stem(stem).exists():
            n = 2
            while _path_for_stem(f"{stem}-{n}").exists():
                n += 1
            stem = f"{stem}-{n}"

    appearance = ctk_appearance if ctk_appearance in ("light", "dark") else "dark"
    payload = {"NAME": name.strip() or stem,
               "CTK_APPEARANCE": appearance,
               "PALETTE": full}
    path = _path_for_stem(stem)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
                    encoding="utf-8")
    return CUSTOM_PREFIX + stem


def delete_custom_theme(theme_id: str) -> bool:
    """Delete a custom theme's JSON file. Returns True if a file was removed."""
    if not is_custom_theme(theme_id):
        return False
    path = _path_for_stem(_stem_from_id(theme_id))
    try:
        path.unlink()
        return True
    except FileNotFoundError:
        return False
    except Exception as exc:
        print(f"[custom_themes] failed to delete {path.name}: {exc}", flush=True)
        return False
