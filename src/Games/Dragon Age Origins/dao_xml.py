"""
dao_xml.py
Deploy-time generation of Dragon Age: Origins registry XML.

DAO discovers installed DLC-style content (AddIns and Offers) through two
registry files the game reads at launch:

    Settings/AddIns.xml   - <AddInsList> of every installed AddIn
    Settings/Offers.xml   - <OfferList>  of every installed Offer

Each .dazip ships a Manifest.xml whose AddInItem/OfferItem must be merged into
these lists, or the content is installed on disk but invisible in-game.

CRITICAL: a fresh install's AddIns.xml already lists the official DLC AddInItems
(Awakening, The Stone Prisoner, Return to Ostagar, ...). Those entries drive the
"New Game → Awakening" flow and the "Downloadable Content" menu. So we must NOT
rebuild the registry from mod Manifests alone - that wipes the DLC list. Instead
we snapshot the pristine registry on the FIRST deploy (``*.mm_vanilla``), seed
each rebuild from that snapshot, then merge the enabled mods on top. Restore
copies the snapshot back verbatim. On an install already broken by a pre-fix
deploy (no snapshot, DLC already gone) we recover the official DLC list by
scanning the game install's own ``addins/``/``offers/`` Manifests - the same
source DAO's DAUpdater uses.

``RequiresAuthorization="1"`` is rewritten to ``"0"`` so the game does not gate
the content behind an online/DLC-key check.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from xml.etree import ElementTree as ET

# (subdir, manifest item tag, list root tag, output filename, gold baseline)
# The gold files (DAO_Addins.xml / DAO_Offers.xml) are vendored from the MO2 DAO
# plugin: a hand-curated, complete list of every official DAO AddIn/Offer/promo
# DLC. They are the authoritative, disk-independent baseline every deploy seeds
# from, so the official DLC list (Awakening, the DLC menu, ...) is always intact
# even on an install whose own AddIns.xml is already broken or missing.
_REGISTRIES = (
    ("AddIns", "AddInItem", "AddInsList", "AddIns.xml", "DAO_Addins.xml"),
    ("Offers", "OfferItem", "OfferList", "Offers.xml", "DAO_Offers.xml"),
)

# Directory holding this module - the vendored gold baseline files live here.
_THIS_DIR = Path(__file__).resolve().parent


def _gold_items(gold_name: str, item_tag: str, list_tag: str) -> dict:
    """Read the bundled gold-baseline DLC items, keyed by UID."""
    return _read_items(_THIS_DIR / gold_name, item_tag, list_tag)

# Suffix for the pristine (vanilla) copy of each registry file. The first time
# we touch a registry we stash the untouched original here so the official DLC
# AddInItems it carries (Awakening, Stone Prisoner, Return to Ostagar, ...) are
# never lost - the deployed registry is vanilla + mods, and restore copies the
# vanilla file back verbatim.
_VANILLA_SUFFIX = ".mm_vanilla"


def _vanilla_path(out_path: Path) -> Path:
    return out_path.with_name(out_path.name + _VANILLA_SUFFIX)


def _ensure_vanilla_backup(out_path: Path, log_fn=None) -> None:
    """Stash the pristine registry once, before we ever rewrite it.

    Only the FIRST call (when no backup exists yet) copies - subsequent deploys
    must not overwrite the vanilla snapshot with an already-modded file.
    """
    _log = log_fn or (lambda _: None)
    vanilla = _vanilla_path(out_path)
    if vanilla.exists():
        return
    if out_path.exists():
        shutil.copy2(out_path, vanilla)
        _log(f"  [DAO] backed up vanilla {out_path.name} → {vanilla.name}")
    else:
        # No registry shipped (e.g. Offers.xml on some installs). Record an
        # empty-baseline marker so restore knows the vanilla state was "absent".
        vanilla.write_text("", encoding="utf-8")


def _read_items(path: Path, item_tag: str, list_tag: str) -> dict:
    """Read AddInItem/OfferItem elements from a registry file, keyed by UID."""
    items: dict[str, ET.Element] = {}
    if not path.exists() or path.stat().st_size == 0:
        return items
    try:
        root = ET.parse(path).getroot()
    except ET.ParseError:
        return items
    container = root if root.tag == list_tag else root.find(list_tag)
    if container is None:
        return items
    for item in container.findall(item_tag):
        uid = item.get("UID")
        if uid:
            items[uid] = item
    return items


def _vanilla_items(out_path: Path, item_tag: str, list_tag: str) -> dict:
    """Read items from the vanilla snapshot, keyed by UID."""
    return _read_items(_vanilla_path(out_path), item_tag, list_tag)


def _iter_manifests(search_root: Path, subdir: str):
    """Yield every Manifest.xml that sits under an ``<subdir>/`` path segment.

    Walks recursively so it works both for the flat deployed layout
    (``<data_root>/addins/<uid>/Manifest.xml``) and the nested staging layout
    (``<staging>/<mod>/addins/<uid>/Manifest.xml``). Matching is by the
    presence of the subdir name (case-insensitive) in the path.
    """
    import os
    want = subdir.casefold()
    if not search_root.is_dir():
        return
    # A Profile Group's staging is a farm of per-mod symlinks; os.walk never
    # descends into those, so expand them to their real targets first.
    from Utils.profile_groups import staging_walk_roots
    seen: set[str] = set()
    for base in staging_walk_roots(search_root):
        for dirpath, _dn, fns in os.walk(base):
            for fn in fns:
                if fn.casefold() != "manifest.xml":
                    continue
                parts = [p.casefold() for p in Path(dirpath).parts]
                if want not in parts:
                    continue
                found = Path(dirpath) / fn
                key = str(found)
                if key in seen:
                    continue
                seen.add(key)
                yield found


def _baseline_items(out_path: Path, subdir: str, item_tag: str, list_tag: str,
                    gold_name: str, recovery_root: "Path | None",
                    log_fn=None) -> dict:
    """Seed the registry baseline with the official DLC list.

    Source priority (all unioned by UID, later sources only fill gaps):
      1. Vendored gold file (DAO_Addins.xml / DAO_Offers.xml) - the authoritative,
         disk-independent list of every official AddIn/Offer/promo DLC. Primary.
      2. The pristine snapshot captured on this install's first deploy - catches
         any DLC the user had that isn't in gold (region/promo variants).
      3. On the very first deploy (no snapshot), the current live registry.
      4. The game install's own addins/offers Manifests - last-ditch recovery.

    The gold file alone is enough to keep Awakening / the DLC menu working even on
    an install whose own AddIns.xml is already broken or missing.
    """
    _log = log_fn or (lambda _: None)

    # 1. Gold baseline - always present, authoritative.
    items = _gold_items(gold_name, item_tag, list_tag)
    if items:
        _log(f"  [DAO] {out_path.name}: seeded {len(items)} official DLC "
             f"item(s) from bundled baseline.")

    def _fill_gaps(src: dict, label: str) -> None:
        added = 0
        for uid, item in src.items():
            if uid not in items:
                items[uid] = item
                added += 1
        if added:
            _log(f"  [DAO] {out_path.name}: +{added} item(s) from {label}.")

    # 2. Snapshot (if any), else 3. the live registry on first deploy.
    if _vanilla_path(out_path).exists():
        _fill_gaps(_vanilla_items(out_path, item_tag, list_tag),
                   "vanilla snapshot")  # i18n: skip — log source name
    else:
        _fill_gaps(_read_items(out_path, item_tag, list_tag),
                   "live registry")  # i18n: skip — log source name

    # 4. Game-install Manifests (recovery), if a game path was provided.
    if recovery_root is not None and recovery_root.is_dir():
        game_items: dict = {}
        for manifest in _iter_manifests(recovery_root, subdir):
            try:
                root = ET.parse(manifest).getroot()
            except ET.ParseError:
                continue
            container = root.find(list_tag)
            if container is None:
                continue
            for item in container.findall(item_tag):
                uid = item.get("UID")
                if uid:
                    game_items[uid] = item
        _fill_gaps(game_items, "game install")  # i18n: skip — log source name

    return items


def build_registry_xml(data_root: Path, mod_staging: "Path | None" = None,
                       game_path: "Path | None" = None, log_fn=None) -> int:
    """(Re)build Settings/AddIns.xml and Settings/Offers.xml from Manifests.

    data_root   - DAO data folder (output Settings/ lives here)
    mod_staging - optional staging root; Manifests are read from here so the
                  registry reflects every enabled mod even when Manifest.xml is
                  not deployed into the data folder. Falls back to data_root.
    Returns the total number of registry items written across both files.
    """
    _log = log_fn or (lambda _: None)
    settings_dir = data_root / "Settings"
    settings_dir.mkdir(parents=True, exist_ok=True)

    search_root = mod_staging if mod_staging and mod_staging.is_dir() else data_root

    total = 0
    for subdir, item_tag, list_tag, out_name, gold_name in _REGISTRIES:
        out_path = settings_dir / out_name
        # Seed the official DLC baseline (gold + snapshot/live + game install) so
        # the DLC AddInItems (Awakening, DLC menu, ...) survive every deploy.
        items = _baseline_items(out_path, subdir, item_tag, list_tag,
                                gold_name, game_path, log_fn=_log)
        if items and not _vanilla_path(out_path).exists():
            # Heal: persist the baseline AS the registry first, so the snapshot we
            # capture below is the correct vanilla (official-DLC) state.
            _write_list(out_path, list_tag, list(items.values()))
        _ensure_vanilla_backup(out_path, log_fn=_log)
        vanilla_count = len(items)

        # Merge enabled mods' Manifests on top (last wins per UID).
        for manifest in _iter_manifests(search_root, subdir):
            try:
                root = ET.parse(manifest).getroot()
            except ET.ParseError as exc:
                _log(f"  [DAO] skipping bad Manifest {manifest}: {exc}")
                continue
            container = root.find(list_tag)
            if container is None:
                continue
            for item in container.findall(item_tag):
                uid = item.get("UID")
                if uid:
                    items[uid] = item

        _write_list(out_path, list_tag, list(items.values()))
        mod_count = len(items) - vanilla_count
        _log(
            f"  [DAO] {out_name}: wrote {len(items)} item(s) "
            f"({vanilla_count} vanilla + {mod_count} mod)."
        )
        total += len(items)

    return total


def _write_list(out_path: Path, list_tag: str, items: list[ET.Element]) -> None:
    """Write a registry file containing list_tag with the given items."""
    root = ET.Element(list_tag)
    for item in items:
        root.append(item)
    xml_str = ET.tostring(root, encoding="unicode")
    xml_str = xml_str.replace(
        'RequiresAuthorization="1"', 'RequiresAuthorization="0"'
    )
    out_path.write_text(
        '<?xml version="1.0" encoding="utf-8"?>\n' + xml_str,
        encoding="utf-8",
    )


def reset_registry_xml(data_root: Path, log_fn=None) -> None:
    """Restore AddIns.xml / Offers.xml to their pristine vanilla state.

    Copies the snapshot captured on first deploy back verbatim, so the official
    DLC AddInItems return exactly as they shipped. The snapshot is consumed
    (removed) so the next deploy re-snapshots a clean baseline.
    """
    _log = log_fn or (lambda _: None)
    settings_dir = data_root / "Settings"
    if not settings_dir.is_dir():
        return
    for _subdir, _item_tag, list_tag, out_name, _gold in _REGISTRIES:
        out_path = settings_dir / out_name
        vanilla = _vanilla_path(out_path)
        if vanilla.exists():
            if vanilla.stat().st_size == 0:
                # Vanilla had no such registry - remove ours to match.
                if out_path.exists():
                    out_path.unlink()
                _log(f"  [DAO] {out_name}: vanilla had none - removed.")
            else:
                shutil.copy2(vanilla, out_path)
                _log(f"  [DAO] restored vanilla {out_name} from snapshot.")
            vanilla.unlink()
            continue
        # No snapshot (legacy/already-broken install). Fall back to an empty
        # list rather than leaving stale mod entries behind.
        if out_name == "Offers.xml" and not out_path.exists():
            continue
        _write_list(out_path, list_tag, [])
        _log(f"  [DAO] {out_name}: no vanilla snapshot - reset to empty list.")
