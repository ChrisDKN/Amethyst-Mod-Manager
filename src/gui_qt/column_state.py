"""Modlist column-state persistence: widths/order/hidden/sort saved to a
``[qt_columns]`` section of amethyst.ini, keyed by column NAME (not index) so it
never collides with the Tk app's index-based ``[columns]`` section.
"""

from __future__ import annotations

import configparser

from Utils.ui_config import get_ui_config_path
from Utils.atomic_write import write_atomic_text

_SECTION = "qt_columns"


def _make_parser():
    # strict=False so a legacy duplicate key (e.g. an older build wrote the
    # lowercase ``w_mod name`` AND this case-preserving build wrote ``w_Mod
    # Name``) doesn't raise on read - last value wins. save_state rewrites the
    # whole section from scratch, which purges such stale duplicates.
    parser = configparser.ConfigParser(strict=False)
    parser.optionxform = str   # preserve key case (column names are case-sensitive)
    return parser


def _read():
    path = get_ui_config_path()
    parser = _make_parser()
    if path.is_file():
        try:
            parser.read(path)
        except Exception:
            pass
    return parser


def _write(parser):
    import io
    buf = io.StringIO()
    parser.write(buf)
    try:
        write_atomic_text(get_ui_config_path(), buf.getvalue())
    except Exception:
        pass


def save_state(widths: dict[str, int], order: list[str],
               hidden: set[str], sort_col: str | None, ascending: bool,
               section: str = _SECTION) -> None:
    parser = _read()
    # Rewrite the section from scratch so stale keys (incl. legacy lower-cased
    # ``w_*`` duplicates from older builds) are removed rather than accumulated.
    if parser.has_section(section):
        parser.remove_section(section)
    parser.add_section(section)
    sec = parser[section]
    for name, w in widths.items():
        sec[f"w_{name}"] = str(int(w))
    sec["order"] = ",".join(order)
    sec["hidden"] = ",".join(sorted(hidden))
    sec["sort_col"] = sort_col or ""
    sec["sort_asc"] = "1" if ascending else "0"
    _write(parser)


def load_state(section: str = _SECTION, columns: list[str] | None = None):
    """Return dict(widths, order, hidden, sort_col, ascending) or empty defaults."""
    parser = _read()
    out = {"widths": {}, "order": [], "hidden": set(),
           "sort_col": None, "ascending": True}
    if not parser.has_section(section):
        return out
    sec = parser[section]
    # Map width keys case-insensitively back to canonical column names. A
    # ui_config write (default, lower-casing optionxform) shares this file and
    # can lower-case our ``w_Mod Name`` → ``w_mod name``; resolve against the
    # real column names so widths survive that round-trip instead of silently
    # resetting. Unknown keys fall back to their raw (case-preserved) name.
    if columns is None:
        from gui_qt.modlist_model import COLUMNS as columns
    _canon = {c.lower(): c for c in columns}
    for key, val in sec.items():
        if key.startswith("w_"):
            try:
                raw_name = key[2:]
                out["widths"][_canon.get(raw_name.lower(), raw_name)] = int(val)
            except ValueError:
                pass
    if sec.get("order"):
        out["order"] = [s for s in sec["order"].split(",") if s]
    if sec.get("hidden"):
        out["hidden"] = {s for s in sec["hidden"].split(",") if s}
    out["sort_col"] = sec.get("sort_col") or None
    out["ascending"] = sec.get("sort_asc", "1") != "0"
    return out
