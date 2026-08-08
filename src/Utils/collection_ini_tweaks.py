"""Apply ``INI Tweaks/*.ini`` shipped with a Nexus collection archive.

Tweak filenames carry the target INI name in brackets, e.g.::

    GTS Changes [SkyrimPrefs].ini   -> targets SkyrimPrefs.ini
    My Tweaks    [Skyrim].ini       -> targets Skyrim.ini

Operation is **non-destructive to the user's prefix**:

  1. If the profile doesn't have a copy of the target INI yet, copy the user's
     live INI from the prefix's My Games folder into the profile (so the
     profile has the user's pre-tweaks content as its baseline).
  2. Merge each ``[Section] key = value`` from the tweak file into the
     profile copy: keys with a different value get **updated**, missing keys
     get **added**, identical keys are left alone.
  3. Every change is logged: ``added``, ``changed``, or ``unchanged``.

The deploy step (``_symlink_profile_ini_files`` on Bethesda games) handles
linking the profile's INI into the prefix at runtime, so we never write the
user's live prefix INI from this module.
"""

from __future__ import annotations

import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable


# Per-game whitelist of valid target INI filenames. Mirrors the same table
# Vortex uses (extensions/collections/src/initweaks.ts gameSettingsFiles).
# Lower-cased for matching; original casing preserved when written.
GAME_INI_TARGETS: dict[str, list[str]] = {
    "Skyrim Special Edition": ["Skyrim.ini", "SkyrimPrefs.ini", "SkyrimCustom.ini"],
    "Skyrim": ["Skyrim.ini", "SkyrimPrefs.ini"],
    "Skyrim VR": ["Skyrim.ini", "SkyrimPrefs.ini"],
    "Enderal": ["Enderal.ini", "EnderalPrefs.ini"],
    "Enderal Special Edition": ["Enderal.ini", "EnderalPrefs.ini"],
    "Fallout 3": ["Fallout.ini", "FalloutPrefs.ini", "FalloutCustom.ini"],
    "Fallout New Vegas": ["Fallout.ini", "FalloutPrefs.ini"],
    "Fallout 4": ["Fallout4.ini", "Fallout4Prefs.ini", "Fallout4Custom.ini"],
    "Fallout 4 VR": ["Fallout4Custom.ini", "Fallout4Prefs.ini"],
    "Starfield": ["StarfieldCustom.ini", "StarfieldPrefs.ini"],
    "Oblivion": ["Oblivion.ini"],
    "Morrowind": ["Morrowind.ini"],
}

# Tweak filenames carry the target name in brackets at the end, e.g.
# "GTS Changes [SkyrimPrefs].ini". The regex captures the LAST bracketed
# group before ".ini"; we then validate the captured stem against the
# game's whitelist so misleading filenames like "Tweaks [v2].ini" are
# rejected instead of silently producing "v2.ini".
_TARGET_RE = re.compile(r"\[([^\]]+)\]\.ini$", re.IGNORECASE)


@dataclass
class IniTweakResult:
    files_processed: int = 0
    keys_added: int = 0
    keys_changed: int = 0
    keys_unchanged: int = 0
    skipped: int = 0
    profile_inis_imported: list[str] = field(default_factory=list)


def _target_ini_for_tweak(
    tweak_path: Path, allowed: list[str] | None = None,
) -> str | None:
    """Extract the target INI filename from a tweak's filename.

    e.g. ``"GTS Changes [SkyrimPrefs].ini"`` → ``"SkyrimPrefs.ini"``.

    When ``allowed`` is supplied, only target stems matching one of the
    listed filenames (case-insensitive) are accepted; this rejects
    misleading suffixes like ``"My Tweaks [Skyrim] [v2].ini"`` that would
    otherwise resolve to ``v2.ini``. Returns ``None`` on any mismatch.
    """
    m = _TARGET_RE.search(tweak_path.name)
    if not m:
        return None
    candidate = m.group(1) + ".ini"
    if allowed is not None:
        allowed_lower = {a.lower(): a for a in allowed}
        match = allowed_lower.get(candidate.lower())
        if match is None:
            return None
        return match
    return candidate


def _parse_ini_kv(text: str) -> dict[str, dict[str, str]]:
    """Return a ``{section: {key: value}}`` dict for the tweak.

    Comment-only lines and blanks are ignored. Section/key matching is
    case-insensitive on lookup but original casing is preserved on emit.
    """
    sections: dict[str, dict[str, str]] = {}
    cur: str | None = None
    section_re = re.compile(r"^\s*\[(?P<name>[^\]]+)\]\s*$")
    kv_re = re.compile(r"^\s*(?P<key>[^=;#][^=]*?)\s*=\s*(?P<val>.*?)\s*$")
    for raw in text.splitlines():
        line = raw.rstrip("\r")
        m = section_re.match(line)
        if m:
            cur = m.group("name").strip()
            sections.setdefault(cur, {})
            continue
        if cur is None:
            continue
        km = kv_re.match(line)
        if km:
            sections[cur][km.group("key").strip()] = km.group("val").strip()
    return sections


def apply_collection_ini_tweaks(
    archive_root: Path,
    profile_dir: Path,
    prefix_ini_dir: Path | None,
    set_ini_key: Callable[[Path, str, str, "str | None"], None],
    read_ini_key: Callable[[Path, str, str], "str | None"],
    log_fn: Callable[[str], None] | None = None,
    allowed_targets: list[str] | None = None,
) -> IniTweakResult:
    """Apply every ``.ini`` under ``<archive_root>/INI Tweaks/`` to profile copies.

    Parameters
    ----------
    archive_root
        Extracted collection archive directory (contains ``INI Tweaks/``).
    profile_dir
        Active profile dir; profile-side INI copies live directly here.
    prefix_ini_dir
        The user's prefix My Games dir (e.g. ``<prefix>/drive_c/users/steamuser/
        Documents/My Games/Skyrim Special Edition``). Used to seed the profile
        copy on first apply. May be ``None`` - in that case profile copies
        start from a stub if absent.
    set_ini_key, read_ini_key
        Re-uses the byte-preserving helpers in ``Bethesda.py`` so we don't
        duplicate INI parsing.
    """
    log = log_fn or (lambda *_a: None)
    result = IniTweakResult()

    tweaks_dir = archive_root / "INI Tweaks"
    if not tweaks_dir.is_dir():
        return result

    tweak_files = sorted(p for p in tweaks_dir.iterdir() if p.is_file() and p.suffix.lower() == ".ini")
    if not tweak_files:
        return result

    log(f"Collection INI tweaks: found {len(tweak_files)} tweak file(s)")

    for tweak_path in tweak_files:
        target_name = _target_ini_for_tweak(tweak_path, allowed=allowed_targets)
        if not target_name:
            if allowed_targets is not None:
                log(f"Collection INI tweaks: skipping '{tweak_path.name}' - "
                    f"target not in allowlist {allowed_targets}")
            else:
                log(f"Collection INI tweaks: skipping '{tweak_path.name}' - "
                    "filename doesn't end with [TargetIni].ini")
            result.skipped += 1
            continue

        profile_ini = profile_dir / target_name

        # Seed the profile INI from the user's live prefix INI on first apply.
        if not profile_ini.exists():
            seeded = False
            if prefix_ini_dir is not None:
                src = prefix_ini_dir / target_name
                if src.is_file():
                    try:
                        profile_dir.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(src, profile_ini)
                        seeded = True
                        result.profile_inis_imported.append(target_name)
                        log(f"Collection INI tweaks: imported '{target_name}' "
                            f"from prefix into profile")
                    except Exception as exc:
                        log(f"Collection INI tweaks: failed to seed "
                            f"'{target_name}' from prefix: {exc}")
            if not seeded:
                # Create an empty file so set_ini_key has something to write to.
                profile_dir.mkdir(parents=True, exist_ok=True)
                profile_ini.touch()
                log(f"Collection INI tweaks: created empty '{target_name}' in "
                    "profile (no prefix copy found)")

        # Parse the tweak and apply each key.
        try:
            text = tweak_path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            text = tweak_path.read_text(encoding="utf-8", errors="replace")
        sections = _parse_ini_kv(text)

        log(f"Collection INI tweaks: applying '{tweak_path.name}' → "
            f"profile/{target_name}")
        for section, kvs in sections.items():
            for key, new_value in kvs.items():
                current = read_ini_key(profile_ini, section, key)
                if current is None:
                    set_ini_key(profile_ini, section, key, new_value)
                    result.keys_added += 1
                    log(f"  added   [{section}] {key} = {new_value}")
                elif current.strip() == new_value.strip():
                    result.keys_unchanged += 1
                else:
                    set_ini_key(profile_ini, section, key, new_value)
                    result.keys_changed += 1
                    log(f"  changed [{section}] {key}: {current!r} → {new_value!r}")
        result.files_processed += 1

    return result
