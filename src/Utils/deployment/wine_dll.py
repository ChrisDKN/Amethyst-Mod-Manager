"""
deployment/wine_dll.py
Wine / Proton DLL override management and emergency deployed-file cleanup.

Extracted from the deployment façade during the 2026-04 refactor.
"""

from __future__ import annotations

import os
import shutil
import time as _time
from pathlib import Path

from Utils.app_log import safe_log as _safe_log
from Utils.atomic_write import write_atomic_text


# ---------------------------------------------------------------------------
# Emergency cleanup - remove hardlinked / symlinked mod files
# ---------------------------------------------------------------------------

def remove_deployed_files(game_dir: Path, log_fn=None) -> int:
    """Remove all hardlinked and symlinked files from *game_dir* recursively,
    then rename any ``*_Core`` vanilla-backup directories back to their
    original names and prune empty directories.

    This is an emergency recovery tool for situations where the mod manager's
    own restore cannot run (e.g. the profile was deleted or the modlist is
    missing).  It works by detecting files that were placed by the deploy step:

    * **Symlinks** - trivially identifiable; always removed.
    * **Hardlinks** - identified by ``st_nlink > 1``: the file has more than
      one directory entry pointing at it, meaning the mod staging copy and the
      game-folder copy share the same inode.  A vanilla file that was never
      hardlinked will have ``st_nlink == 1``.

    After removing deployed files, any sibling ``*_Core`` directory (e.g.
    ``Data_Core/``) is renamed back to its original name (``Data/``), merging
    into any remaining content.  The same rename pass is also applied to
    ``*_Core`` subdirectories found directly inside *game_dir* (covers UE5
    games where the game root itself is scanned).

    Empty sub-directories left behind after removal are pruned.

    Returns the number of files removed.
    """
    def _log(msg: str) -> None:
        if log_fn:
            log_fn(msg)

    removed = 0
    if not game_dir.is_dir():
        _log(f"Directory not found: {game_dir}")
        return 0

    # --- Step 1: remove deployed files ---
    for root, dirs, files in os.walk(game_dir, topdown=False, followlinks=False):
        root_path = Path(root)
        for fname in files:
            fpath = root_path / fname
            try:
                if fpath.is_symlink():
                    fpath.unlink()
                    _log(f"Removed symlink: {fpath}")
                    removed += 1
                elif fpath.stat().st_nlink > 1:
                    fpath.unlink()
                    _log(f"Removed hardlink: {fpath}")
                    removed += 1
            except OSError as exc:
                _log(f"Could not remove {fpath}: {exc}")
        # Prune empty directories (skip the root itself)
        if root_path != game_dir:
            try:
                root_path.rmdir()   # only succeeds if empty
            except OSError:
                pass

    _log(f"Removed {removed} deployed file(s) from {game_dir}")

    # --- Step 2: rename *_Core backup dirs back to their original names ---
    # Collect candidate directories to check:
    #   • Siblings of game_dir whose name ends with "_Core" and whose stripped
    #     name matches game_dir's name (e.g. Data_Core → Data).
    #   • Direct children of game_dir ending with "_Core" (UE5 / game-root scan).
    def _rename_core_dirs(search_parent: Path) -> None:
        try:
            entries = list(search_parent.iterdir())
        except OSError:
            return
        for entry in entries:
            if not entry.is_dir():
                continue
            name = entry.name
            if not name.endswith("_Core"):
                continue
            original_name = name[: -len("_Core")]
            target = entry.parent / original_name
            if target.exists():
                # Merge: move contents of core dir into target, then remove core dir
                for src in list(entry.rglob("*")):
                    if not src.is_file():
                        continue
                    rel = src.relative_to(entry)
                    dst = target / rel
                    if not dst.exists():
                        dst.parent.mkdir(parents=True, exist_ok=True)
                        shutil.move(str(src), str(dst))
                        _log(f"Restored vanilla file: {dst}")
                shutil.rmtree(entry, ignore_errors=True)
                _log(f"Merged {entry.name}/ into {original_name}/")
            else:
                try:
                    entry.rename(target)
                    _log(f"Renamed {entry.name}/ → {original_name}/")
                except OSError as exc:
                    _log(f"Could not rename {entry}: {exc}")

    # Siblings (Bethesda / BepInEx style: Data_Core/ next to Data/)
    _rename_core_dirs(game_dir.parent)
    # Children (UE5 style: game root contains subdirs that may have _Core siblings)
    _rename_core_dirs(game_dir)

    # --- Step 3: prune empty directories left inside game_dir ---
    for root, dirs, files in os.walk(game_dir, topdown=False, followlinks=False):
        root_path = Path(root)
        if root_path != game_dir and not files and not dirs:
            try:
                root_path.rmdir()
            except OSError:
                pass

    return removed


# ---------------------------------------------------------------------------
# Wine / Proton prefix helpers
# ---------------------------------------------------------------------------

def apply_wine_dll_overrides(
    prefix_path: Path,
    overrides: dict[str, str],
    log_fn=None,
) -> None:
    """Write DLL override entries into the Proton prefix's user.reg.

    *prefix_path* is the ``pfx/`` directory (the one that contains
    ``drive_c/`` and ``user.reg``).

    *overrides* maps DLL name → load order string, e.g.
    ``{"winhttp": "native,builtin"}``.

    The function locates (or creates) the
    ``[Software\\\\Wine\\\\DllOverrides]`` section in ``user.reg`` and
    inserts/updates each key.  The file is written atomically so a crash
    mid-write cannot corrupt the prefix.

    If *prefix_path* does not exist or ``user.reg`` cannot be read the
    call is a silent no-op (logged as a warning).
    """
    _log = _safe_log(log_fn)

    if not overrides:
        return

    # Accept either the pfx/ directory directly or its parent (compatdata/<id>/)
    if not (prefix_path / "user.reg").is_file() and (prefix_path / "pfx" / "user.reg").is_file():
        prefix_path = prefix_path / "pfx"
    user_reg = prefix_path / "user.reg"
    if not user_reg.is_file():
        _log(f"Warning: user.reg not found at {user_reg}; skipping DLL overrides.")
        return

    try:
        text = user_reg.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        _log(f"Warning: could not read user.reg: {exc}")
        return

    lines = text.splitlines(keepends=True)
    section_header = "[Software\\\\Wine\\\\DllOverrides]"

    # Locate the section (case-insensitive header match)
    section_start: int | None = None
    section_end: int | None = None  # index of first line after this section
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.lower().startswith(section_header.lower()):
            section_start = i
        elif section_start is not None and stripped.startswith("["):
            section_end = i
            break

    # Timestamps used when creating a new section or adding new entries only.
    # Section headers in Wine's .reg format use a decimal Unix timestamp;
    # #time= lines use a hex Windows FILETIME (100ns ticks since 1601-01-01).
    _unix_ts   = int(_time.time())
    _filetime_hex = format(int((_unix_ts + 11644473600) * 1e7), "x")

    if section_start is None:
        # Section doesn't exist - append it at the end
        _log(r"[Software\\Wine\\DllOverrides] not found; appending to user.reg.")
        if lines and not lines[-1].endswith("\n"):
            lines.append("\n")
        lines.append("\n")
        lines.append(f"{section_header} {_unix_ts}\n")
        lines.append(f"#time={_filetime_hex}\n")
        for dll, value in sorted(overrides.items()):
            lines.append(f'"{dll}"="{value}"\n')
            _log(f"  DLL override set: {dll} = {value}")
    else:
        # Section exists - find existing keys and add/update
        body_start = section_start + 1
        body_end = section_end if section_end is not None else len(lines)
        key_lines = lines[body_start:body_end]

        # Separate trailing blank lines (section terminators in Wine's .reg
        # format) from the actual key/value body.  New entries must go into
        # the body, never after the trailing blanks.
        trailing: list[str] = []
        while key_lines and not key_lines[-1].strip():
            trailing.insert(0, key_lines.pop())

        def _sorted_insert_pos(entry_line: str) -> int:
            """Return the index where *entry_line* should be inserted to keep
            the section body in alphabetical order (matching winecfg behaviour).
            Skips non-key lines (#time=, blank, etc.)."""
            entry_key = entry_line.split("=", 1)[0].strip().lower()
            for idx, kl in enumerate(key_lines):
                kl_stripped = kl.strip()
                if not kl_stripped or kl_stripped.startswith("#"):
                    continue
                existing_key = kl_stripped.split("=", 1)[0].strip().lower()
                if existing_key > entry_key:
                    return idx
            return len(key_lines)

        changed = False
        for dll, value in overrides.items():
            key_lower = f'"{dll.lower()}"'
            expected_line = f'"{dll}"="{value}"\n'
            found_at: int | None = None
            for j, kline in enumerate(key_lines):
                if kline.lower().startswith(key_lower + "="):
                    found_at = j
                    break
            if found_at is not None:
                if key_lines[found_at] == expected_line:
                    pass  # correct value, correct position - nothing to do
                else:
                    # Value is wrong - update in place (position is already sorted)
                    key_lines[found_at] = expected_line
                    changed = True
                    _log(f"  DLL override updated: {dll} = {value}")
            else:
                # New entry - insert in sorted position (matching winecfg)
                pos = _sorted_insert_pos(expected_line)
                key_lines.insert(pos, expected_line)
                changed = True
                _log(f"  DLL override set: {dll} = {value}")

        if not changed:
            # All overrides already present with the correct values - leave
            # user.reg completely untouched so Wine's own state is preserved.
            _log("  DLL overrides already set correctly; skipping write.")
            return

        # Re-append trailing blank lines to preserve section terminator.
        key_lines.extend(trailing)

        # Only update the section header / #time= timestamps when we actually
        # make a change, and use the formats Wine expects.
        lines[section_start] = f"{section_header} {_unix_ts}\n"
        for j, kline in enumerate(key_lines):
            if kline.lower().startswith("#time="):
                key_lines[j] = f"#time={_filetime_hex}\n"
                break

        lines[body_start:body_end] = key_lines

    try:
        write_atomic_text(user_reg, "".join(lines))
    except OSError as exc:
        _log(f"Warning: could not write user.reg: {exc}")


def set_show_dot_files(prefix_path: Path, log_fn=None) -> bool:
    """Set ``[Software\\\\Wine]`` ``"ShowDotFiles"="Y"`` in the prefix's user.reg.

    This is winecfg's "Show dot files" checkbox: without it Wine hides every
    Unix dotfile/dot-directory, so file dialogs inside the prefix cannot browse
    to ``~/.local/share/...`` or ``~/.config/...`` - which is where the mod
    manager keeps profiles, staged mods and tool data.  Tools that ask the user
    to pick a Data folder are unusable in a prefix without it.

    Written straight into user.reg rather than through ``reg add`` because the
    caller may be on the UI thread: a ``reg add`` costs a full wine startup
    (seconds), while this is a small text edit.  Returns True when the value is
    already correct or was written, False when the prefix couldn't be touched.

    Wine reads user.reg when a wineserver starts and rewrites it on shutdown,
    so this must run while no process is live in the prefix - which is the case
    at env-resolution time, before the tool is launched.
    """
    _log = _safe_log(log_fn)

    # Accept either the pfx/ directory directly or its parent (compatdata/<id>/)
    prefix_path = Path(prefix_path)
    if not (prefix_path / "user.reg").is_file() and (prefix_path / "pfx" / "user.reg").is_file():
        prefix_path = prefix_path / "pfx"
    user_reg = prefix_path / "user.reg"
    if not user_reg.is_file():
        _log(f"Show dot files: user.reg not found at {user_reg}; skipping.")
        return False

    try:
        text = user_reg.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        _log(f"Show dot files: could not read user.reg: {exc}")
        return False

    lines = text.splitlines(keepends=True)
    section_header = "[Software\\\\Wine]"
    entry_line = '"ShowDotFiles"="Y"\n'

    # Locate the section. The header match must be exact up to the closing
    # bracket - a plain startswith() would also match [Software\\Wine\\Explorer]
    # and friends, and the value would land in the wrong key.
    section_start: int | None = None
    section_end: int | None = None  # index of first line after this section
    for i, line in enumerate(lines):
        stripped = line.strip()
        if section_start is None:
            if stripped.lower().startswith(section_header.lower() + " ") \
                    or stripped.lower() == section_header.lower():
                section_start = i
        elif stripped.startswith("["):
            section_end = i
            break

    _unix_ts = int(_time.time())
    _filetime_hex = format(int((_unix_ts + 11644473600) * 1e7), "x")

    if section_start is None:
        if lines and not lines[-1].endswith("\n"):
            lines.append("\n")
        lines.append("\n")
        lines.append(f"{section_header} {_unix_ts}\n")
        lines.append(f"#time={_filetime_hex}\n")
        lines.append(entry_line)
        _log(r"Show dot files: added [Software\\Wine] to user.reg.")
    else:
        body_start = section_start + 1
        body_end = section_end if section_end is not None else len(lines)
        key_lines = lines[body_start:body_end]

        # Trailing blanks terminate the section in Wine's .reg format - a new
        # entry has to go before them, not after.
        trailing: list[str] = []
        while key_lines and not key_lines[-1].strip():
            trailing.insert(0, key_lines.pop())

        found_at: int | None = None
        for j, kline in enumerate(key_lines):
            if kline.strip().lower().startswith('"showdotfiles"='):
                found_at = j
                break

        if found_at is not None and key_lines[found_at] == entry_line:
            # Already correct - leave user.reg completely untouched so Wine's
            # own state (and its mtime) is preserved.
            return True
        if found_at is not None:
            key_lines[found_at] = entry_line
        else:
            key_lines.append(entry_line)

        key_lines.extend(trailing)
        lines[section_start] = f"{section_header} {_unix_ts}\n"
        for j, kline in enumerate(key_lines):
            if kline.lower().startswith("#time="):
                key_lines[j] = f"#time={_filetime_hex}\n"
                break
        lines[body_start:body_end] = key_lines
        _log("Show dot files: enabled in user.reg.")

    try:
        write_atomic_text(user_reg, "".join(lines))
    except OSError as exc:
        _log(f"Show dot files: could not write user.reg: {exc}")
        return False
    return True


def remove_wine_dll_overrides(
    prefix_path: Path,
    dlls: "list[str] | set[str]",
    log_fn=None,
) -> None:
    """Remove Wine DLL override entries from the Proton prefix's user.reg.

    *dlls* is a collection of DLL names whose ``[Software\\\\Wine\\\\DllOverrides]``
    entries should be deleted.  Entries not present in the file are silently
    skipped.  The file is written atomically.
    """
    _log = _safe_log(log_fn)

    if not dlls:
        return

    dlls_lower = {d.lower() for d in dlls}

    # Accept either the pfx/ directory directly or its parent
    if not (prefix_path / "user.reg").is_file() and (prefix_path / "pfx" / "user.reg").is_file():
        prefix_path = prefix_path / "pfx"
    user_reg = prefix_path / "user.reg"
    if not user_reg.is_file():
        _log(f"Warning: user.reg not found at {user_reg}; skipping DLL override removal.")
        return

    try:
        text = user_reg.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        _log(f"Warning: could not read user.reg: {exc}")
        return

    lines = text.splitlines(keepends=True)
    section_header = "[Software\\\\Wine\\\\DllOverrides]"

    section_start: int | None = None
    section_end: int | None = None
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.lower().startswith(section_header.lower()):
            section_start = i
        elif section_start is not None and stripped.startswith("["):
            section_end = i
            break

    if section_start is None:
        return  # section doesn't exist, nothing to remove

    body_start = section_start + 1
    body_end = section_end if section_end is not None else len(lines)
    key_lines = lines[body_start:body_end]

    removed_count = 0
    new_key_lines = []
    for kline in key_lines:
        stripped = kline.strip()
        if stripped.startswith('"'):
            # Extract the key name between the first pair of quotes
            end_quote = stripped.find('"', 1)
            if end_quote > 1:
                key_name = stripped[1:end_quote].lower()
                if key_name in dlls_lower:
                    _log(f"  DLL override removed: {stripped[1:end_quote]}")
                    removed_count += 1
                    continue  # drop this line
        new_key_lines.append(kline)

    if removed_count == 0:
        return  # nothing actually changed - leave user.reg untouched

    # Fix up the section header and #time= timestamps to use the formats
    # Wine expects: decimal Unix seconds for the header, hex Windows FILETIME
    # for the #time= line.
    _unix_ts = int(_time.time())
    _filetime_hex = format(int((_unix_ts + 11644473600) * 1e7), "x")
    lines[section_start] = f"{section_header} {_unix_ts}\n"
    for j, kline in enumerate(new_key_lines):
        if kline.lower().startswith("#time="):
            new_key_lines[j] = f"#time={_filetime_hex}\n"
            break

    lines[body_start:body_end] = new_key_lines

    try:
        write_atomic_text(user_reg, "".join(lines))
    except OSError as exc:
        _log(f"Warning: could not write user.reg: {exc}")


__all__ = [
    "remove_deployed_files",
    "apply_wine_dll_overrides",
    "remove_wine_dll_overrides",
    "set_show_dot_files",
]
