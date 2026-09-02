"""Neutral (GUI-free) logic for the BG3 Mod Manager load-order import wizard.

Extracted from the Tk ``bg3_import_modlist_json`` plugin.  Converts a BG3MM
``modlist.json`` (or exported saved-order .json), or the game's own
``modsettings.lsx``, into this profile's ``modlist.txt`` order by matching each
order entry's pak UUID against the UUIDs scanned out of the staged mods.

BG3MM/modsettings.lsx is lowest-priority-first; our modlist.txt is
highest-priority-first, so the matched run is reversed when written.

No tkinter or Qt imports - the Qt/Tk views only handle file-picking and the
preview textbox; all the parsing/matching/planning lives here so it can be
unit-tested headlessly.
"""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

from Utils.mods.modlist import ModEntry, read_modlist, write_modlist
from Utils.bg3.modsettings import _SYSTEM_UUIDS, _repair_meta_xml, scan_mod_paks


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
# Parsing modsettings.lsx
# ---------------------------------------------------------------------------

def _lsx_attr(node: ET.Element, attr_id: str) -> str:
    """Return the value of <attribute id="attr_id" value="X"/> under *node*."""
    for attr in node.findall("attribute"):
        if attr.get("id") == attr_id:
            return (attr.get("value") or "").strip()
    return ""


def parse_modsettings_lsx(path: Path) -> list[tuple[str, str]]:
    """Return an ordered list of (uuid, name) from a ``modsettings.lsx``.

    Reads the ``ModuleShortDesc`` entries under the ``Mods`` node, which is the
    game's own load order - lowest-priority-first, same convention as a BG3MM
    order .json, so the result feeds ``plan_reorder`` unchanged.

    Base-game modules (GustavX/Shared/DiceSet/... and any Adventure campaign
    entry sitting in the first slot) are dropped: they are engine entries, not
    installed mods, and would otherwise show up as "in file but not installed".
    Patch 6 files also carry a ``ModOrder`` node of bare UUID references; the
    ``Mods`` node is already in load order, so ModOrder is ignored.
    """
    text = Path(path).read_text(encoding="utf-8-sig", errors="replace")
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        root = ET.fromstring(_repair_meta_xml(text))

    # Locate the Mods node; fall back to scanning every ModuleShortDesc when a
    # hand-edited file nests things differently.
    mods_node = None
    for node in root.iter("node"):
        if node.get("id") == "Mods":
            mods_node = node
            break
    scope = mods_node if mods_node is not None else root

    result: list[tuple[str, str]] = []
    seen: set[str] = set()
    for node in scope.iter("node"):
        if node.get("id") != "ModuleShortDesc":
            continue
        uuid = _lsx_attr(node, "UUID")
        if not uuid or uuid in _SYSTEM_UUIDS:
            continue
        key = uuid.casefold()
        if key in seen:
            continue
        seen.add(key)
        result.append((uuid, _lsx_attr(node, "Name")))

    # A custom campaign (Adventure) mod also sits in the first slot, replacing
    # GustavX. It is left in: unlike the stock campaign it IS an installed mod,
    # so it should be matched and positioned like any other.
    return result


def parse_order_file(path: Path) -> list[tuple[str, str]]:
    """Parse a BG3MM order .json or a modsettings.lsx - dispatch on content.

    Sniffing beats the extension: users rename these files, and a mis-typed
    suffix should still import rather than throw a parser error.
    """
    p = Path(path)
    head = p.read_text(encoding="utf-8-sig", errors="replace")[:512].lstrip()
    if head.startswith("<"):
        return parse_modsettings_lsx(p)
    if head.startswith(("{", "[")):
        return parse_order_json(p)
    # No usable marker - go by extension, then try the other parser.
    if p.suffix.lower() == ".lsx":
        return parse_modsettings_lsx(p)
    try:
        return parse_order_json(p)
    except Exception:
        return parse_modsettings_lsx(p)


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
    """Compute (new_entries, extra_mod_names, missing_order_entries).

    Each installed mod is positioned by where its UUID first appears in
    *order_uuids*; a folder with several paks sorts to the earliest position
    among them; a folder with no pak UUID falls back to a case-insensitive Name
    match.  Installed mods absent from the order file are placed above the
    imported run, UNTOUCHED (enabled state is left exactly as it was).
    A BG3MM/Vortex export or a modsettings.lsx only lists pak-module UUIDs -
    script extender/native-loader/config installs never have a pak at all, and
    override-only paks are excluded too, so "absent from the order file" carries
    no information about the user's intent and must not be treated as "disable
    this." The matched run is reversed (both source formats are
    lowest-priority-first, modlist.txt is highest-priority-first).
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
    extra: list[str]                    # installed but not in the order file
    missing: list[tuple[str, str]]      # in the order file but not installed
    order_count: int                    # total entries in the order file
    modlist_path: Path


def compute_import_plan(game, json_path: Path,
                        profile_name: str = "") -> ImportPlan:
    """Read the order file + scan staging + plan the reorder.  Raises on error
    (no order entries / undeterminable profile).

    *json_path* may be a BG3MM order .json or a modsettings.lsx.
    """
    order_uuids = parse_order_file(json_path)
    if not order_uuids:
        raise RuntimeError("No mod entries found in that file.")

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
            lines.append(f"   • {e.name}   [not in order file – left {state}]")
        else:
            idx += 1
            lines.append(f"{idx:>3}. {e.name}")
    if plan.missing:
        lines.append("")
        lines.append("=== IN ORDER FILE BUT NOT INSTALLED (skipped) ===")
        for uuid, name in plan.missing:
            lines.append(f"   {name or '(unnamed)'}   [{uuid}]")
    return summary, "\n".join(lines)


def apply_plan(plan: ImportPlan) -> Path:
    """Write the new order to the profile's modlist.txt; return its path."""
    write_modlist(plan.modlist_path, plan.new_entries)
    return plan.modlist_path
