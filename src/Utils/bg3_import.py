"""Neutral (GUI-free) logic for the BG3 Mod Manager load-order import wizard.

Extracted from the Tk ``bg3_import_modlist_json`` plugin.  Converts a BG3MM
``modlist.json`` (or exported saved-order .json) into this profile's
``modlist.txt`` order by matching each order entry's pak UUID against the UUIDs
scanned out of the staged mods.

BG3MM/modsettings.lsx is lowest-priority-first; our modlist.txt is
highest-priority-first, so the matched run is reversed when written.

No tkinter or Qt imports - the Qt/Tk views only handle file-picking and the
preview textbox; all the parsing/matching/planning lives here so it can be
unit-tested headlessly.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from Utils.modlist import ModEntry, read_modlist, write_modlist
from Utils.modsettings import scan_mod_paks


# ---------------------------------------------------------------------------
# Parsing the BG3MM JSON
# ---------------------------------------------------------------------------

def parse_order_json(path: Path) -> list[tuple[str, str]]:
    """Return an ordered list of (uuid, name) from a BG3MM order .json.

    Supports the two shapes BG3MM writes:
      1. A DivinityLoadOrder object:  {"Name": ..., "Order": [{"UUID","Name"}, ...]}
      2. A bare exported list:        [{"UUID"/"Uuid", "Name"}, ...]
    UUID/Name keys are matched case-insensitively.
    """
    raw = json.loads(Path(path).read_text(encoding="utf-8-sig"))

    if isinstance(raw, dict):
        order = raw.get("Order") or raw.get("order") or []
    elif isinstance(raw, list):
        order = raw
    else:
        order = []

    result: list[tuple[str, str]] = []
    for item in order:
        if not isinstance(item, dict):
            continue
        uuid = ""
        name = ""
        for k, v in item.items():
            kl = k.lower()
            if kl == "uuid" and isinstance(v, str):
                uuid = v.strip()
            elif kl == "name" and isinstance(v, str):
                name = v.strip()
        if uuid:
            result.append((uuid, name))
    return result


# ---------------------------------------------------------------------------
# Resolving the active profile + staging
# ---------------------------------------------------------------------------

def resolve_profile_modlist(game, profile_name: str = "") -> Path | None:
    """Path to the target profile's modlist.txt, or None if undeterminable.

    Prefers the game's currently-active profile dir; falls back to a
    *profile_name* under ``get_profile_root()/profiles``, then to the recorded
    last-active profile.
    """
    profile_dir = getattr(game, "_active_profile_dir", None)
    if profile_dir is None and profile_name:
        try:
            profile_dir = game.get_profile_root() / "profiles" / profile_name
        except Exception:
            profile_dir = None
    if profile_dir is None:
        try:
            name = game.get_last_active_profile()
            profile_dir = game.get_profile_root() / "profiles" / name
        except Exception:
            return None
    return Path(profile_dir) / "modlist.txt"


def scan_staging_uuids(game, modlist_path: Path
                       ) -> tuple[dict[str, str], dict[str, list[str]]]:
    """Scan all enabled+disabled staged mods for their pak UUIDs.

    Returns ``(uuid_to_mod, mod_to_uuids)``.  Folders with no .pak / no meta.lsx
    UUID are absent from ``mod_to_uuids`` (the caller uses that to fall back to a
    name match).  Disabled mods are scanned too so an imported order can
    re-enable a mod the user had turned off.
    """
    staging = game.get_effective_mod_staging_path()
    entries = read_modlist(modlist_path)
    mod_entries = [e for e in entries if not e.is_separator]
    by_uuid = scan_mod_paks(staging, mod_entries)

    uuid_to_mod: dict[str, str] = {}
    mod_to_uuids: dict[str, list[str]] = {}
    for uuid, info in by_uuid.items():
        mod = info.source_mod
        if not mod:
            continue
        uuid_to_mod[uuid] = mod
        mod_to_uuids.setdefault(mod, []).append(uuid)
    return uuid_to_mod, mod_to_uuids


# ---------------------------------------------------------------------------
# Reorder logic
# ---------------------------------------------------------------------------

def plan_reorder(
    existing: list[ModEntry],
    order_uuids: list[tuple[str, str]],
    uuid_to_mod: dict[str, str],
    mod_to_uuids: dict[str, list[str]],
) -> tuple[list[ModEntry], list[str], list[tuple[str, str]]]:
    """Compute (new_entries, extra_mod_names, missing_json_entries).

    Each installed mod is positioned by where its UUID first appears in the
    JSON's Order array; a folder with several paks sorts to the earliest
    position among them; a folder with no pak UUID falls back to a
    case-insensitive Name match.  Installed mods absent from the JSON are placed
    above the imported run, UNTOUCHED (enabled state is left exactly as it was).
    A BG3MM/Vortex order export only lists pak-module UUIDs it tracks - script
    extender/native-loader/config installs never have a pak at all, and some
    override-only paks are excluded too, so "absent from the JSON" carries no
    information about the user's intent and must not be treated as "disable
    this." The matched run is reversed (JSON is lowest-priority-first,
    modlist.txt is highest-priority-first).
    """
    separators = [e for e in existing if e.is_separator]
    mods = {e.name: e for e in existing if not e.is_separator}

    uuid_pos: dict[str, int] = {}
    name_pos: dict[str, int] = {}
    for i, (uuid, name) in enumerate(order_uuids):
        if uuid and uuid not in uuid_pos:
            uuid_pos[uuid] = i
        if name and name.casefold() not in name_pos:
            name_pos[name.casefold()] = i

    mod_pos: dict[str, int] = {}
    for name in mods:
        positions = [uuid_pos[u] for u in mod_to_uuids.get(name, [])
                     if u in uuid_pos]
        if positions:
            mod_pos[name] = min(positions)
        elif not mod_to_uuids.get(name):
            fallback = name_pos.get(name.casefold())
            if fallback is not None:
                mod_pos[name] = fallback

    ordered_names = sorted(mod_pos, key=lambda n: mod_pos[n])
    matched_set = set(ordered_names)

    placed_uuids = {u for n in ordered_names for u in mod_to_uuids.get(n, [])}
    placed_names = {n.casefold() for n in ordered_names}
    missing: list[tuple[str, str]] = []
    for uuid, name in order_uuids:
        if uuid in placed_uuids:
            continue
        if name and name.casefold() in placed_names:
            continue
        if uuid_to_mod.get(uuid):
            continue
        missing.append((uuid, name))

    extra = [n for n in mods if n not in matched_set]

    new_entries: list[ModEntry] = list(separators)
    for n in extra:
        # Not in the JSON - keep its current enabled/disabled state as-is;
        # the JSON has no opinion on it (see plan_reorder docstring).
        new_entries.append(mods[n])
    for n in reversed(ordered_names):
        e = mods[n]
        e.enabled = True
        e.locked = False
        new_entries.append(e)

    return new_entries, extra, missing


# ---------------------------------------------------------------------------
# Orchestration + preview
# ---------------------------------------------------------------------------

@dataclass
class ImportPlan:
    new_entries: list[ModEntry]
    extra: list[str]                    # installed but not in JSON (disabled)
    missing: list[tuple[str, str]]      # in JSON but not installed
    order_count: int                    # total entries in the JSON
    modlist_path: Path


def compute_import_plan(game, json_path: Path,
                        profile_name: str = "") -> ImportPlan:
    """Read the JSON + scan staging + plan the reorder.  Raises on error
    (no order entries / undeterminable profile)."""
    order_uuids = parse_order_json(json_path)
    if not order_uuids:
        raise RuntimeError("No mod entries found in that JSON.")

    modlist_path = resolve_profile_modlist(game, profile_name)
    if modlist_path is None:
        raise RuntimeError("Could not determine the active profile.")

    existing = read_modlist(modlist_path)
    uuid_to_mod, mod_to_uuids = scan_staging_uuids(game, modlist_path)
    new_entries, extra, missing = plan_reorder(
        existing, order_uuids, uuid_to_mod, mod_to_uuids)
    return ImportPlan(new_entries, extra, missing, len(order_uuids),
                      modlist_path)


def format_preview(plan: ImportPlan) -> tuple[str, str]:
    """Return (summary_line, detail_text) describing *plan* for display."""
    matched = plan.order_count - len(plan.missing)
    summary = (f"{matched} of {plan.order_count} order entries matched "
               f"installed mods.   {len(plan.extra)} extra installed mod(s) "
               f"not in the order (left as-is).   {len(plan.missing)} not "
               f"installed.")

    lines: list[str] = ["=== NEW LOAD ORDER (top = highest priority) ==="]
    extra_set = set(plan.extra)
    idx = 0
    for e in plan.new_entries:
        if e.is_separator:
            lines.append(f"   --- {e.display_name} ---")
        elif e.name in extra_set:
            state = "enabled" if e.enabled else "disabled"
            lines.append(f"   • {e.name}   [not in JSON – left {state}]")
        else:
            idx += 1
            lines.append(f"{idx:>3}. {e.name}")
    if plan.missing:
        lines.append("")
        lines.append("=== IN JSON BUT NOT INSTALLED (skipped) ===")
        for uuid, name in plan.missing:
            lines.append(f"   {name or '(unnamed)'}   [{uuid}]")
    return summary, "\n".join(lines)


def apply_plan(plan: ImportPlan) -> Path:
    """Write the new order to the profile's modlist.txt; return its path."""
    write_modlist(plan.modlist_path, plan.new_entries)
    return plan.modlist_path
