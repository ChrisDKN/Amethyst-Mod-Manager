"""
modlist.py
Read and write a MO2-compatible modlist.txt file.

Format (one mod per line):
  +ModName          — enabled mod
  -ModName          — disabled mod
  *ModName          — enabled, always-on (cannot be toggled)
  +Name_separator   — separator (MO2 sometimes writes these with +)
  -Name_separator   — separator (canonical form, written with - prefix)

Priority: line 0 (top) = highest priority, last line = priority 0.
Separators do not count toward priority numbering.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from Utils.app_log import app_log
from Utils.atomic_write import write_atomic_text

_SEPARATOR_SUFFIX = "_separator"


@dataclass
class ModEntry:
    name: str
    enabled: bool        # + or *  (always True for separators)
    locked: bool         # * prefix — cannot be toggled
    is_separator: bool = field(default=False)

    @property
    def display_name(self) -> str:
        """Human-readable name: strip _separator suffix for separators."""
        if self.is_separator and self.name.endswith(_SEPARATOR_SUFFIX):
            return self.name[: -len(_SEPARATOR_SUFFIX)]
        return self.name

    @property
    def bundle_name(self) -> str | None:
        """Bundle group name if this entry is a bundle variant, else None.

        Bundle variants are stored as ``<bundle_name>__<variant_name>`` in
        modlist.txt.  The double-underscore is the delimiter.
        """
        if self.is_separator:
            return None
        if "__" in self.name:
            return self.name.split("__", 1)[0]
        return None

    @property
    def variant_name(self) -> str | None:
        """Variant name within bundle, or None if not a bundle variant."""
        if self.is_separator:
            return None
        if "__" in self.name:
            return self.name.split("__", 1)[1]
        return None


def _is_separator(name: str) -> bool:
    return name.endswith(_SEPARATOR_SUFFIX)


def read_modlist(modlist_path: Path) -> list[ModEntry]:
    """
    Parse modlist.txt and return entries in file order (index 0 = highest priority).
    Lines that are blank or don't start with +/-/* are skipped.
    """
    entries: list[ModEntry] = []
    if not modlist_path.is_file():
        return entries
    # surrogateescape: imported MO2 modlists can carry non-UTF-8 (e.g. cp1252)
    # bytes in mod names; a strict read would raise on every profile load.
    # Only line endings are stripped — mod folder names may legitimately
    # start/end with spaces and must round-trip unchanged.
    for line in modlist_path.read_text(
            encoding="utf-8", errors="surrogateescape").splitlines():
        line = line.rstrip("\r\n")
        if not line:
            continue
        prefix = line[0]
        name = line[1:]
        if not name:
            continue
        if prefix == "+":
            entries.append(ModEntry(name=name, enabled=True, locked=False,
                                    is_separator=_is_separator(name)))
        elif prefix == "-":
            if _is_separator(name):
                entries.append(ModEntry(name=name, enabled=True, locked=True,
                                        is_separator=True))
            else:
                entries.append(ModEntry(name=name, enabled=False, locked=False,
                                        is_separator=False))
        elif prefix == "*":
            entries.append(ModEntry(name=name, enabled=True,  locked=True,
                                    is_separator=False))
        # else: ignore unknown lines
    return entries


def write_modlist(modlist_path: Path, entries: list[ModEntry]) -> None:
    """
    Write entries back to modlist.txt.
    Creates parent directories if needed. Atomic (write-temp → rename) so a
    concurrent reader never observes a truncated/partial modlist.
    """
    lines = []
    for e in entries:
        if e.is_separator:
            prefix = "-"          # separators always written with -
        elif e.locked:
            prefix = "*"
        elif e.enabled:
            prefix = "+"
        else:
            prefix = "-"
        lines.append(f"{prefix}{e.name}")
    write_atomic_text(modlist_path,
                      "\n".join(lines) + ("\n" if lines else ""),
                      errors="surrogateescape")


def prepend_mod(modlist_path: Path, mod_name: str, enabled: bool = True,
                preserve_existing_state: bool = False) -> None:
    """
    Add a new mod at the top of modlist.txt (highest priority).
    If an entry with the same name already exists it is moved to the top.

    When ``preserve_existing_state`` is True and an entry with the same name
    already exists, its current enabled/locked flags are kept (so reinstalling
    or updating a disabled mod does not silently re-enable it). ``enabled`` is
    only used for brand-new entries in that case.
    """
    entries = read_modlist(modlist_path)
    existing = next((e for e in entries if e.name == mod_name), None)
    # Remove any existing entry with the same name
    entries = [e for e in entries if e.name != mod_name]
    if preserve_existing_state and existing is not None:
        new_entry = ModEntry(name=mod_name, enabled=existing.enabled,
                             locked=existing.locked)
    else:
        new_entry = ModEntry(name=mod_name, enabled=enabled, locked=False)
    entries.insert(0, new_entry)
    write_modlist(modlist_path, entries)


def ensure_mod_preserving_position(
    modlist_path: Path,
    mod_name: str,
    enabled: bool = True,
    preserve_existing_state: bool = True,
) -> None:
    """
    Ensure a mod exists in modlist.txt without changing its existing position.

    If an entry with the same name already exists, its order is preserved. When
    ``preserve_existing_state`` is True (the default for reinstalls/updates) the
    existing entry's enabled/locked flags are left untouched, so updating a
    disabled mod does not silently re-enable it. Otherwise the enabled flag is
    set to ``enabled``. If no entry exists, the mod is added at the top (highest
    priority), matching prepend_mod's behaviour for new mods.
    """
    entries = read_modlist(modlist_path)
    for e in entries:
        if e.name == mod_name:
            if not preserve_existing_state:
                e.enabled = enabled
            write_modlist(modlist_path, entries)
            return

    # If not already present, add as a new top-priority entry.
    entries.insert(0, ModEntry(name=mod_name, enabled=enabled, locked=False))
    write_modlist(modlist_path, entries)


def move_mods_to_anchor(
    modlist_path: Path,
    mod_names: list[str],
    anchor: str | None = None,
    after: bool = False,
    at_end: bool = False,
) -> bool:
    """Move existing entries to a new position as one block, keeping each
    entry's enabled/locked flags. Used by drag-install from the Downloads tab:
    the freshly installed mods (prepended at the top by _add_to_modlist) are
    repositioned to where the archives were dropped.

    The block is inserted directly before ``anchor`` (or after it when
    ``after`` is True — reverse-priority display), or appended at the bottom
    when ``at_end`` is True. Anchoring is by NAME, not index: rows can shift
    between the drop and the end of an install batch. If the anchor entry no
    longer exists (removed/renamed meanwhile, or it is itself one of the moved
    mods) the modlist is left untouched — the mods stay at the top, the normal
    install position. Returns True if the file was rewritten.
    """
    names = [n for n in mod_names if n]
    if not names or (anchor is None and not at_end):
        return False
    entries = read_modlist(modlist_path)
    moving = set(names)
    block = [e for e in entries if e.name in moving]
    if not block:
        return False
    # Keep the caller's order for the block where possible (install order).
    order = {n: i for i, n in enumerate(names)}
    block.sort(key=lambda e: order.get(e.name, len(order)))
    rest = [e for e in entries if e.name not in moving]
    if at_end:
        idx = len(rest)
    else:
        idx = next((i for i, e in enumerate(rest) if e.name == anchor), None)
        if idx is None:
            return False
        if after:
            idx += 1
    write_modlist(modlist_path, rest[:idx] + block + rest[idx:])
    return True


# Profile-root infrastructure folder names. If one of these turns up *inside*
# the mods/ staging folder it's almost always stray/test pollution (a mirror of
# the profile-root layout), never a real mod — so the sync must never adopt it
# into modlist.txt. Case-insensitive match.
_RESERVED_STAGING_NAMES = frozenset({
    "mods", "overwrite", "profiles", "backups",
    "root_folder", "applications",
})


def sync_modlist_with_mods_folder(modlist_path: Path, mods_dir: Path) -> None:
    """Sync modlist_path against mods_dir:

      - Prepend any mod folders not yet in modlist as disabled entries.
      - Remove any non-separator entries whose folder no longer exists.

    Skips MO2 separator dummy folders (_separator suffix) and profile-root
    infrastructure folder names (see _RESERVED_STAGING_NAMES). Creates
    modlist_path if it does not exist. Pure pathlib — no GUI toolkit — so both
    the Tk and Qt Refresh paths can call it (the Tk add-game dialog re-imports
    it from here).
    """
    if not mods_dir.is_dir():
        if not modlist_path.exists():
            modlist_path.touch()
        return

    # Normalise mod folder names that Windows/Wine path resolution would mangle
    # (leading/trailing whitespace, trailing dots, reserved characters). Such a
    # folder desyncs from the modlist: the folder name is unaddressable to
    # Wine tools, and the
    # index/filemap read the raw folder name — so build_filemap's
    # index.get(name) misses and the mod drops out of filemap.txt entirely (no
    # conflicts, no Data-tab rows, no plugins). Rename each such folder to the
    # SAME sanitized name the installer uses (sanitize_mod_folder_name), at the
    # source, so the folder, the index, and every downstream reader agree. Skip
    # when a folder with the sanitized name already exists (a distinct mod) to
    # avoid clobbering it, and skip the "Mod"/DOS-name fallbacks (never rename a
    # folder to a generic placeholder).
    try:
        from Utils.mod_name_utils import sanitize_mod_folder_name
    except Exception:
        sanitize_mod_folder_name = None
    if sanitize_mod_folder_name is not None:
        try:
            for d in list(mods_dir.iterdir()):
                if not d.is_dir() or d.name.endswith("_separator"):
                    continue
                clean = sanitize_mod_folder_name(d.name)
                # No change, or the sanitizer bailed to its placeholder fallback
                # (empty/reserved → "Mod"): leave the folder alone.
                if clean == d.name or clean == "Mod":
                    continue
                target = mods_dir / clean
                if target.exists():
                    app_log(f"Modlist sync: mod folder {d.name!r} needs "
                            f"normalising but {clean!r} already exists — NOT "
                            f"renamed (rename one manually; the malformed copy "
                            f"will not deploy).")
                    continue
                try:
                    d.rename(target)
                    app_log(f"Modlist sync: renamed mod folder {d.name!r} → "
                            f"{clean!r} (the original name would desync the "
                            f"modlist/index and is unreachable to Wine tools).")
                except OSError as exc:
                    app_log(f"Modlist sync: could not rename {d.name!r} → "
                            f"{clean!r}: {exc}")
        except OSError:
            pass

    on_disk: set[str] = {
        d.name for d in mods_dir.iterdir()
        if d.is_dir()
        and not d.name.endswith("_separator")
        and d.name.lower() not in _RESERVED_STAGING_NAMES
    }

    # Parse existing modlist lines, dropping entries whose folder is gone.
    existing_lines: list[str] = []
    existing_names: set[str] = set()
    dropped: list[str] = []
    if modlist_path.exists():
        for line in modlist_path.read_text(
                encoding="utf-8", errors="surrogateescape").splitlines():
            stripped = line.rstrip("\r\n")
            if not stripped.strip():
                continue
            if stripped[0] in ("+", "-", "*"):
                name = stripped[1:]
                # Keep separators always; only keep mods that exist on disk.
                if name.endswith("_separator") or name in on_disk:
                    existing_lines.append(stripped)
                    existing_names.add(name)
                else:
                    dropped.append(name)
            else:
                existing_lines.append(stripped)

    # Safety: dropping an entry also loses its enabled bit — unrecoverable. A
    # few missing folders is a real manual delete; MOST of the modlist missing
    # from mods_dir means mods_dir itself resolved to the wrong folder (e.g.
    # the shared mods/ while a profile-specific-mods profile is active,
    # because a background worker left game._active_profile_dir stale). Never
    # rewrite the modlist from a view of the wrong staging folder.
    existing_mod_count = len(dropped) + sum(
        1 for l in existing_lines
        if l[0] in ("+", "-", "*") and not l[1:].endswith("_separator"))
    if len(dropped) >= 5 and len(dropped) * 2 >= existing_mod_count:
        app_log(f"Modlist sync ABORTED: {len(dropped)} of {existing_mod_count} "
                f"mod entr(y/ies) have no folder under '{mods_dir}' — staging "
                f"path desync suspected; modlist.txt left untouched.")
        return

    new_mods = sorted(on_disk - existing_names)
    new_lines = [f"-{name}" for name in new_mods]

    if new_mods or dropped:
        _added_names = (f" +[{', '.join(new_mods[:5])}"
                        + ("…]" if len(new_mods) > 5 else "]")) if new_mods else ""
        _dropped_names = (f" -[{', '.join(dropped[:5])}"
                          + ("…]" if len(dropped) > 5 else "]")) if dropped else ""
        app_log(f"Modlist sync: +{len(new_mods)} added (disabled), "
                f"-{len(dropped)} removed{_added_names}{_dropped_names}")

    all_lines = new_lines + existing_lines
    write_atomic_text(modlist_path,
                      "\n".join(all_lines) + ("\n" if all_lines else ""),
                      errors="surrogateescape")
