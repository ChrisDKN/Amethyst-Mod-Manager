"""Pure helpers for the Change Version / mod-files picker.

Toolkit-neutral (no tkinter, no Qt) so both the Tk overlay
(`gui/mod_files_overlay.py`) and the Qt overlay (`gui_qt/change_version_view.py`)
can share the highlight + formatting logic. Extracted verbatim from the original
Tk overlay.
"""

from __future__ import annotations


def resolve_latest_name_match(files, installed_file_id: int,
                              fallback_name: str) -> tuple[int, set[int]]:
    """Resolve the file the Change Version window highlights orange.

    Returns ``(match_id, old_match_ids)`` where ``match_id`` is the newest file
    whose display name matches the installed file's name (or *fallback_name* if
    the installed file isn't in the list), or ``-1`` when there is no name match.
    ``old_match_ids`` are the other same-name files (the red "old match" rows)."""
    installed_file = next(
        (f for f in files
         if installed_file_id > 0 and f.file_id == installed_file_id),
        None,
    )
    # Prefer the installed file's name over the local folder name because the
    # user may have renamed the mod on install.
    target = _normalize_match(
        (installed_file.name or installed_file.file_name)
        if installed_file else fallback_name
    )
    match_id = -1
    old_match_ids: set[int] = set()
    if target:
        name_matches = [f for f in files
                        if _normalize_match(f.name or "") == target]
        if name_matches:
            newest = max(name_matches, key=lambda f: f.uploaded_timestamp or 0)
            match_id = newest.file_id
            old_match_ids = {f.file_id for f in name_matches
                             if f.file_id != match_id}
    return match_id, old_match_ids


def _normalize_match(s: str) -> str:
    """Casefold and collapse whitespace/punctuation so that names like
    'Cargo Reconsidered - Watchtower' and 'Cargo Reconsidered  -  Watchtower'
    compare equal. Returns '' for empty input."""
    if not s:
        return ""
    out = []
    prev_sep = True
    for ch in s.casefold():
        if ch.isalnum():
            out.append(ch)
            prev_sep = False
        else:
            if not prev_sep:
                out.append(" ")
                prev_sep = True
    return "".join(out).strip()


def fmt_size(n_bytes: int) -> str:
    """Format a byte count as a short B/KB/MB/GB string ('' for 0/None)."""
    if not n_bytes:
        return ""
    if n_bytes >= 1_073_741_824:
        return f"{n_bytes / 1_073_741_824:.1f} GB"
    if n_bytes >= 1_048_576:
        return f"{n_bytes / 1_048_576:.1f} MB"
    if n_bytes >= 1_024:
        return f"{n_bytes / 1_024:.0f} KB"
    return f"{n_bytes} B"


# Category sort order for the file list (MAIN first … OLD_VERSION last).
CATEGORY_ORDER = {"MAIN": 0, "UPDATE": 1, "OPTIONAL": 2,
                  "MISCELLANEOUS": 3, "OLD_VERSION": 4}


def sort_key(f) -> tuple[int, int]:
    """Sort key: category order, then newest-first by upload time."""
    return (CATEGORY_ORDER.get((f.category_name or "").upper(), 9),
            -(f.uploaded_timestamp or 0))
