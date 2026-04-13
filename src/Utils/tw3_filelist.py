"""
tw3_filelist.py — Menu Filelist Updater for The Witcher 3: Next-Gen Edition.

The Next-Gen update (4.0+) requires two text files in:
    bin/config/r4game/user_config_matrix/pc/

    dx11filelist.txt  — menu XMLs for DX11 mode
    dx12filelist.txt  — menu XMLs for DX12 mode

Each file is a plain list of XML filenames, one per line, each followed by
a semicolon:
    audio.xml;
    modSomeMenu.xml;
    graphicsdx11.xml;   ← DX11 only (excluded from DX12 list)
    graphics.xml;       ← DX12 only (excluded from DX11 list)

This module reimplements the logic of https://github.com/Aelto/tw3-menufilelist-updater
in pure Python so no external binary is needed.

Behaviour
---------
- If the target directory does not exist the function returns immediately
  (older game versions don't have it; they don't need filelists).
- If neither dx11filelist.txt nor dx12filelist.txt is present the function
  also returns immediately — the directory exists but the game version
  pre-dates the requirement, so we leave it alone.
- Filenames starting with "~" are ignored (backups/temporaries).
- The two vanilla graphics entries are swapped between the two lists as
  described above.
- Both output files are written atomically (via a .tmp sibling) so a crash
  mid-write cannot corrupt them.
- Returns a short human-readable summary string for callers to log.
"""

from __future__ import annotations

from pathlib import Path

from Utils.app_log import safe_log as _safe_log

_MENU_DIR_REL   = Path("bin/config/r4game/user_config_matrix/pc")
_DX11_FILE      = "dx11filelist.txt"
_DX12_FILE      = "dx12filelist.txt"
_DX11_VANILLA   = "graphicsdx11.xml"   # belongs in DX11 list, not DX12
_DX12_VANILLA   = "graphics.xml"       # belongs in DX12 list, not DX11
_IGNORE_PREFIX  = "~"


def update_menu_filelists(game_root: Path, log_fn=None) -> None:
    """Regenerate dx11filelist.txt and dx12filelist.txt under *game_root*.

    Silently skips if the menu directory or neither filelist file exists
    (pre-Next-Gen installs don't need this).

    Parameters
    ----------
    game_root:
        Absolute path to the Witcher 3 installation root.
    log_fn:
        Optional callable(str) for status messages.
    """
    _log = _safe_log(log_fn)
    menu_dir = game_root / _MENU_DIR_REL

    if not menu_dir.is_dir():
        return  # Pre-Next-Gen or non-standard install — skip silently.

    dx11_path = menu_dir / _DX11_FILE
    dx12_path = menu_dir / _DX12_FILE

    if not dx11_path.exists() and not dx12_path.exists():
        return  # Neither filelist present — this game version doesn't need them.

    # Collect all XML filenames, excluding ignored prefixes and the filelist
    # files themselves (they live in the same dir but aren't XML).
    xmls: list[str] = sorted(
        entry.name
        for entry in menu_dir.iterdir()
        if (
            entry.is_file()
            and entry.suffix.lower() == ".xml"
            and not entry.name.startswith(_IGNORE_PREFIX)
        )
    )

    if not xmls:
        return  # No XMLs found at all — skip silently.

    dx11_entries = [x for x in xmls if x != _DX12_VANILLA]
    dx12_entries = [x for x in xmls if x != _DX11_VANILLA]

    _write_filelist(dx11_path, dx11_entries)
    _write_filelist(dx12_path, dx12_entries)

    _log(
        f"TW3 filelists updated: {len(dx11_entries)} DX11 entries, "
        f"{len(dx12_entries)} DX12 entries."
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _write_filelist(path: Path, entries: list[str]) -> None:
    """Write *entries* to *path* atomically as ``name.xml;\n`` lines."""
    content = "".join(f"{e};\n" for e in entries)
    tmp = path.with_suffix(".tmp")
    try:
        tmp.write_text(content, encoding="utf-8")
        tmp.replace(path)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise
