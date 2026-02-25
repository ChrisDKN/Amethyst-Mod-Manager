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
    for line in modlist_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        prefix = line[0]
        name = line[1:]
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
    Creates parent directories if needed.
    """
    modlist_path.parent.mkdir(parents=True, exist_ok=True)
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
    modlist_path.write_text("\n".join(lines) + ("\n" if lines else ""),
                            encoding="utf-8")


def prepend_mod(modlist_path: Path, mod_name: str, enabled: bool = True) -> None:
    """
    Add a new mod at the top of modlist.txt (highest priority).
    If an entry with the same name already exists it is moved to the top.
    """
    entries = read_modlist(modlist_path)
    # Remove any existing entry with the same name
    entries = [e for e in entries if e.name != mod_name]
    entries.insert(0, ModEntry(name=mod_name, enabled=enabled, locked=False))
    write_modlist(modlist_path, entries)
