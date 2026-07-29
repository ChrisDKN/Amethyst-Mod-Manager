"""
GUI-neutral core of the Plugin Audit wizard.

Moved out of wizards/plugin_audit.py (which imports customtkinter) so the Qt
wizard view can share it: the plugin-header/new-record scanners, the
load-order/profile/priority/patch-index helpers, the AuditEntry model, and standalone
scan/disable/cleanup functions ported from the Tk wizard's methods.
"""

from __future__ import annotations

import mmap
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Dict, List, Optional, Set, Tuple

if TYPE_CHECKING:
    from Games.base_game import BaseGame

# Neutral fallback colour for the "Unknown" patch label (was gui.theme.TEXT_DIM).
_NEUTRAL_DIM = "#9aa0a6"

BASE_GAME_PLUGINS = {
    "Skyrim.esm", "Update.esm", "Dawnguard.esm", "HearthFires.esm",
    "Dragonborn.esm", "ccBGSSSE001-Fish.esm",
}

# Patch-type labels and colours
_PATCH_LABELS = {
    "PRE_PATCHED_BOS":       ("Base Object Swapper", "#5ba8e0"),
    "PRE_PATCHED_SP":        ("SkyPatcher",          "#e0a85b"),
    "PRE_PATCHED_SPID":      ("SPID",                "#e0a85b"),
    "PRE_PATCHED_KID":       ("KID",                 "#e0a85b"),
    "PRE_PATCHED_DSD":       ("DSD",                 "#e0a85b"),
    "PRE_PATCHED_OAR":       ("OAR",                 "#e0a85b"),
    "PRE_PATCHED_SYNTHESIS": ("Synthesis",            "#e0a85b"),
    "PRE_PATCHED_BASHED":    ("Bashed Patch",         "#8b5cf6"),
}

SAFE_COLOR   = "#6bc76b"
UNSAFE_COLOR = "#e06c6c"
WARN_COLOR   = "#e0c45b"

# Helpers

def _read_active_profile(game: "BaseGame") -> str:
    active_dir = getattr(game, "_active_profile_dir", None)
    if active_dir is not None:
        name = Path(active_dir).name
        if name:
            return name
    try:
        last = game.get_last_active_profile()
        if last and last != "default":
            candidate = game.get_profile_root() / "profiles" / last
            if candidate.is_dir():
                return last
    except Exception:
        pass
    try:
        profiles_root = game.get_profile_root() / "profiles"
        if profiles_root.is_dir():
            candidates = sorted(
                d for d in profiles_root.iterdir()
                if d.is_dir() and d.name != "default" and (d / "loadorder.txt").is_file()
            )
            if candidates:
                return candidates[0].name
    except Exception:
        pass
    return "default"


def _profile_dir(game: "BaseGame", profile: str) -> Path:
    return game.get_profile_root() / "profiles" / profile


def _read_loadorder(game: "BaseGame", profile: str) -> List[str]:
    pdir = _profile_dir(game, profile)
    lo_path = pdir / "loadorder.txt"
    pl_path = pdir / "plugins.txt"

    full_order: List[str] = []
    if lo_path.is_file():
        for line in lo_path.read_text(encoding="utf-8").splitlines():
            line = line.split("#")[0].strip()
            if line:
                full_order.append(line)

    active_set: Set[str] = set()
    if pl_path.is_file():
        for line in pl_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("*"):
                active_set.add(line[1:].strip())
            elif not line.startswith("#") and line:
                active_set.add(line)

    if not full_order:
        return list(active_set)
    return [p for p in full_order if p in BASE_GAME_PLUGINS or p in active_set]


def _read_modlist_priorities(game: "BaseGame", profile: str) -> Dict[str, int]:
    """{mod_name: priority}. Higher = wins conflicts. modlist.txt line 0 = highest."""
    pdir = _profile_dir(game, profile)
    ml_path = pdir / "modlist.txt"
    if not ml_path.is_file():
        # Fall back to shared modlist
        ml_path = game.get_profile_root() / "profiles" / profile / "modlist.txt"
    if not ml_path.is_file():
        return {}

    real_mods: List[str] = []
    for line in ml_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line[0] not in ("+", "-", "*"):
            continue
        name = line[1:]
        if name.endswith("_separator"):
            continue
        real_mods.append(name)

    total = len(real_mods)
    return {name: (total - 1 - i) for i, name in enumerate(real_mods)}


_plugin_file_cache: Dict[str, Optional[Path]] = {}

def _find_plugin_file(plugin_name: str, mods_path: Path,
                      game: "BaseGame") -> Optional[Path]:
    """Locate plugin file on disk. Result cached per run."""
    key = f"{mods_path}|{plugin_name}"
    if key in _plugin_file_cache:
        return _plugin_file_cache[key]
    result: Optional[Path] = None
    # 1. Inside staging mods
    if mods_path and mods_path.is_dir():
        for mod_dir in mods_path.iterdir():
            if not mod_dir.is_dir():
                continue
            for candidate in (mod_dir / plugin_name, mod_dir / "Data" / plugin_name):
                if candidate.is_file():
                    result = candidate
                    break
            if result:
                break
    # 2. Vanilla data dir
    if result is None:
        try:
            data_path = game.get_game_path() / "Data" / plugin_name
            if data_path.is_file():
                result = data_path
        except Exception:
            pass
    _plugin_file_cache[key] = result
    return result


def _find_mod_folder(plugin_path: Path, mods_path: Path) -> Optional[Path]:
    try:
        rel = plugin_path.relative_to(mods_path)
        return mods_path / rel.parts[0]
    except ValueError:
        return None


def _parse_header(path: Path) -> Tuple[str, List[str]]:
    try:
        with open(path, "rb") as f:
            data = f.read(8192)
        if len(data) < 24 or data[:4] != b"TES4":
            return "Unknown", []
        tes4_size = int.from_bytes(data[4:8], "little")
        offset, end = 24, min(24 + tes4_size, len(data))
        author, masters = "Unknown", []
        while offset < end - 6:
            st = data[offset:offset + 4]
            ss = int.from_bytes(data[offset + 4:offset + 6], "little")
            offset += 6
            if offset + ss > len(data):
                break
            chunk = data[offset:offset + ss]
            if chunk and chunk[-1] == 0:
                chunk = chunk[:-1]
            text = chunk.decode("utf-8", errors="ignore").strip()
            if st == b"CNAM":
                author = text or "Unknown"
            elif st == b"MAST" and text:
                masters.append(text)
            offset += ss
        return author, masters
    except Exception:
        return "Unknown", []


def _plugin_has_new_records(path: Path) -> bool:
    """True if plugin adds FormIDs not owned by any master → cannot disable even with patch."""
    try:
        with open(path, "rb") as f:
            with mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as data:
                return _scan_data_for_new_records(data)
    except OSError:
        return True


def _scan_data_for_new_records(data: bytes) -> bool:
    """Core scan — accepts bytes, mmap, or any buffer object."""

    if len(data) < 28 or data[:4] != b"TES4":
        return True

    tes4_size = int.from_bytes(data[4:8], "little", signed=False)
    if tes4_size < 4:
        return True

    # ESL flag (bit 11) at offset 8–11
    flags = int.from_bytes(data[8:12], "little", signed=False)
    is_esl = bool(flags & 0x00000800)

    # Count masters from MAST subrecords
    num_masters = 0
    offset = 24
    tes4_end = min(24 + tes4_size, len(data))
    while offset <= tes4_end - 6:
        st = data[offset:offset + 4]
        ss = int.from_bytes(data[offset + 4:offset + 6], "little", signed=False)
        offset += 6
        if offset + ss > len(data):
            break
        if st == b"MAST":
            num_masters += 1
        offset += ss

    master_set: Set[int] = set(range(num_masters))

    # Recursively scan GRUP tree for record FormIDs
    def _scan(pos: int, end: int) -> int:
        count = 0
        while pos < end - 20:
            if data[pos:pos + 4] == b"GRUP":
                gs = int.from_bytes(data[pos + 4:pos + 8], "little", signed=False)
                if gs < 20 or pos + gs > end:
                    pos += 1
                    continue
                count += _scan(pos + 20, pos + gs)
                pos += gs
                continue

            sig = data[pos:pos + 4]
            if not all(65 <= b <= 90 for b in sig):
                pos += 1
                continue
            if sig == b"TES4":
                ds = int.from_bytes(data[pos + 4:pos + 8], "little", signed=False)
                pos += 24 + ds
                continue

            ds = int.from_bytes(data[pos + 4:pos + 8], "little", signed=False)
            if ds > 100_000_000:
                pos += 1
                continue

            formid = int.from_bytes(data[pos + 12:pos + 16], "little", signed=False)
            src = (formid >> 24) & 0xFF

            if is_esl:
            # ESL-compacted new records have FormID < 0x1000.
            # Skyrim.esm overrides are >> 0x1000, so this distinguishes them.
                if src == 0x00 and 0 < formid < 0x1000:
                    count += 1
                elif src not in master_set:
                    count += 1
            else:
                if src not in master_set:
                    count += 1

            pos += 24 + ds
            while pos & 3:
                pos += 1

        return count

    new_count = _scan(24 + tes4_size, len(data))
    return new_count > 0


def _build_patch_index(mods_path: Path) -> Tuple[Set[str], Set[str]]:
    """Return (bos_patched, sp_patched) — lower-case plugin name sets."""
    bos_patched: Set[str] = set()
    sp_patched:  Set[str] = set()
    if not mods_path or not mods_path.is_dir():
        return bos_patched, sp_patched
    _for_re = re.compile(r"for\s+([^\s\r\n]+\.es[pml])", re.I)

    try:
        for mod_dir in mods_path.iterdir():
            if not mod_dir.is_dir():
                continue
            bos_dir = mod_dir / "SKSE" / "Plugins" / "Data" / "Base Object Swapper"
            if bos_dir.is_dir():
                for ini in bos_dir.rglob("*.ini"):
                    try:
                        header = ini.read_text(encoding="utf-8", errors="ignore")[:512]
                    except OSError:
                        continue
                    m = _for_re.search(header)
                    if m:
                        bos_patched.add(m.group(1).lower())
                    else:
                        stem = ini.stem.lower()
                        for suffix in ("_skygen_swap", "_swap"):
                            if stem.endswith(suffix):
                                stem = stem[:-len(suffix)]
                                break
                        for ext in (".esp", ".esm", ".esl"):
                            bos_patched.add(stem + ext)
            try:
                for ini in mod_dir.rglob("*_swap.ini"):
                    stem = ini.stem.lower()
                    if stem.endswith("_swap"):
                        stem = stem[:-5]
                    for ext in (".esp", ".esm", ".esl"):
                        bos_patched.add(stem + ext)
            except OSError:
                pass
            for sp_name in ("SkyPatcher", "SkyPatcher2"):
                sp_dir = mod_dir / "SKSE" / "Plugins" / sp_name
                if sp_dir.is_dir():
                    for ini in sp_dir.rglob("*.ini"):
                        try:
                            header = ini.read_text(encoding="utf-8", errors="ignore")[:512]
                        except OSError:
                            continue
                        m = _for_re.search(header)
                        if m:
                            sp_patched.add(m.group(1).lower())
                        else:
                            stem = ini.stem.lower()
                            stem = re.sub(r"^[0-9a-f]+-", "", stem)
                            if stem.endswith("_skygen"):
                                stem = stem[:-7]
                            for ext in (".esp", ".esm", ".esl"):
                                sp_patched.add(stem + ext)
                    break
            # SPID / KID / DSD — scan relevant extensions only
            try:
                for pattern in ("*.ini", "*.json", "*.yaml", "*.yml"):
                    for f in mod_dir.rglob(pattern):
                        if not f.is_file():
                            continue
                        nl = f.name.lower()
                        stem = f.stem.lower()
                        base_stem = stem
                        for suffix in ("_distr", "_kid", "_dsd"):
                            if stem.endswith(suffix):
                                base_stem = stem[:-len(suffix)]
                                break
                        if nl.endswith("_distr.ini"):
                            sp_patched.add(base_stem + ".esp")
                            sp_patched.add(base_stem + ".esm")
                            sp_patched.add(base_stem + ".esl")
                        elif nl.endswith("_kid.ini"):
                            sp_patched.add(base_stem + ".esp")
                            sp_patched.add(base_stem + ".esm")
                            sp_patched.add(base_stem + ".esl")
                        elif nl.endswith("_dsd.json") or nl.endswith("_dsd.yaml"):
                            sp_patched.add(base_stem + ".esp")
                            sp_patched.add(base_stem + ".esm")
                            sp_patched.add(base_stem + ".esl")
            except OSError:
                pass
    except OSError:
        pass
    return bos_patched, sp_patched


def _has_skygen_ini(plugin_name: str, mods_path: Optional[Path]) -> bool:
    """True if a SkyGen INI for this plugin exists in the SkyGen output directories."""
    if not mods_path or not mods_path.is_dir():
        return False
    stem = Path(plugin_name).stem.lower()
    base = mods_path

    # BOS: SkyGen BOS/SKSE/Plugins/Data/Base Object Swapper/{stem}_SkyGen_SWAP.ini
    ini = base / "SkyGen BOS" / "SKSE" / "Plugins" / "Data" / "Base Object Swapper" / f"{stem}_SkyGen_SWAP.ini"
    if ini.is_file():
        return True

    # BOS (legacy naming): {stem}_SkyGen_SWAP.ini anywhere in mod
    bos_mod = base / "SkyGen BOS"
    if bos_mod.is_dir():
        for f in bos_mod.rglob("*.ini"):
            fn = f.name.lower()
            if fn == f"{stem}_skygen_swap.ini" or fn.endswith(f"{stem}_skygen_swap.ini"):
                return True

    # SkyPatcher: uses load-order prefix, so we check within the SP dir
    for sp_dir_name in ("SkyPatcher", "SkyPatcher2"):
        sp_dir = base / "SkyGen SkyPatcher" / "SKSE" / "Plugins" / sp_dir_name
        if not sp_dir.is_dir():
            continue
        for f in sp_dir.rglob("*.ini"):
            fn_lower = f.stem.lower()
            # Strip load-order prefix (e.g. "03-") then check for _skygen
            cleaned = re.sub(r"^[0-9a-f]+-", "", fn_lower)
            if cleaned == f"{stem}_skygen" or cleaned == stem:
                return True
        break

    return False



@dataclass
class AuditEntry:
    plugin_name:    str
    masters:        List[str]
    patch_scents:   Set[str]           # PRE_PATCHED_* flags
    mod_name:       str                # owning mod folder name
    priority:       int                # Amethyst priority (higher = wins)
    dependents:     List[str] = field(default_factory=list)
    transitively_safe: bool = False
    has_new_records: bool = False      # plugin adds its own FormIDs
    patch_is_skygen: bool = False      # patch INI resides in SkyGen output dir

    @property
    def is_patched(self) -> bool:
        return bool(self.patch_scents)

    @property
    def can_disable(self) -> bool:
        if self.has_new_records:
            return False
        return self.is_patched and (not self.dependents or self.transitively_safe)

    @property
    def primary_patch_label(self) -> Tuple[str, str]:
        """(label, colour) for dominant patch type."""
        for key in ("PRE_PATCHED_BOS", "PRE_PATCHED_SP", "PRE_PATCHED_SPID",
                    "PRE_PATCHED_KID", "PRE_PATCHED_DSD", "PRE_PATCHED_OAR",
                    "PRE_PATCHED_SYNTHESIS"):
            if key in self.patch_scents:
                return _PATCH_LABELS[key]
        return ("Unknown", _NEUTRAL_DIM)

    @property
    def unsafe_reason(self) -> str:
        parts: List[str] = []
        if self.dependents and not self.transitively_safe:
            names = ", ".join(self.dependents[:4])
            extra = f" (+{len(self.dependents)-4} more)" if len(self.dependents) > 4 else ""
            parts.append(f"Required by: {names}{extra}")
        if self.has_new_records:
            parts.append("Adds new records (can't be disabled)")

# ---------------------------------------------------------------------------
# Scan / disable / cleanup (ported from the Tk wizard methods)
# ---------------------------------------------------------------------------

def _staging_path(game: "BaseGame") -> Path:
    try:
        return game.get_effective_mod_staging_path()
    except Exception:
        return game.get_mod_staging_path()


def scan_load_order(game: "BaseGame", *,
                    progress_fn: "Callable[[float], None]" = lambda _f: None,
                    log_fn: "Callable[[str], None]" = lambda _m: None,
                    ) -> "Dict[str, AuditEntry] | None":
    """Scan the active profile's load order into an AuditEntry map, or None
    when there are no active plugins. Blocking; call from a worker thread."""
    profile = _read_active_profile(game)
    log_fn(f"Active profile: '{profile}'")
    lo = _read_loadorder(game, profile)
    if not lo:
        return None
    mods_path = _staging_path(game)

    log_fn("Reading mod priorities…")
    priorities = _read_modlist_priorities(game, profile)

    log_fn("Indexing plugin files…")
    _plugin_file_cache.clear()
    if mods_path and mods_path.is_dir():
        for mod_dir in mods_path.iterdir():
            if not mod_dir.is_dir():
                continue
            for candidate in mod_dir.iterdir():
                if candidate.is_file() and candidate.suffix.lower() in (".esp", ".esm", ".esl"):
                    key = f"{mods_path}|{candidate.name}"
                    _plugin_file_cache.setdefault(key, candidate)
            data_sub = mod_dir / "Data"
            if data_sub.is_dir():
                for candidate in data_sub.iterdir():
                    if candidate.is_file() and candidate.suffix.lower() in (".esp", ".esm", ".esl"):
                        key = f"{mods_path}|{candidate.name}"
                        _plugin_file_cache.setdefault(key, candidate)

    log_fn("Building cross-mod patch index…")
    bos_idx, sp_idx = _build_patch_index(mods_path)
    log_fn(f"Patch index: {len(bos_idx)} BOS, {len(sp_idx)} SP/other targets")

    total = len(lo)
    entries: Dict[str, AuditEntry] = {}
    for i, plugin_name in enumerate(lo):
        if i % 10 == 0 or i == total - 1:
            progress_fn((i + 1) / total)
        if i % 20 == 0 or i == total - 1:
            log_fn(f"Scanning {i+1}/{total}: {plugin_name}")

        plugin_path = _find_plugin_file(plugin_name, mods_path, game)
        if plugin_path is None:
            continue
        _, masters = _parse_header(plugin_path)
        mod_folder = _find_mod_folder(plugin_path, mods_path)
        mod_name = mod_folder.name if mod_folder else ""
        priority = priorities.get(mod_name, -1)
        has_new = _plugin_has_new_records(plugin_path)

        scents: Set[str] = set()
        pkey = plugin_name.lower()
        if pkey in bos_idx:
            scents.add("PRE_PATCHED_BOS")
        if pkey in sp_idx:
            scents.add("PRE_PATCHED_SP")
        if plugin_name.lower() in ("synthesis.esp", "synthesis.esm"):
            scents.add("PRE_PATCHED_SYNTHESIS")
        if mod_folder:
            oar_dir = mod_folder / "meshes" / "animationdatasinglefile"
            oar_dir2 = mod_folder / "SKSE" / "Plugins" / "OpenAnimationReplacer"
            if oar_dir.is_dir() or oar_dir2.is_dir():
                scents.add("PRE_PATCHED_OAR")

        entries[plugin_name] = AuditEntry(
            plugin_name=plugin_name, masters=masters, patch_scents=scents,
            mod_name=mod_name, priority=priority, has_new_records=has_new,
            patch_is_skygen=_has_skygen_ini(plugin_name, mods_path))

    # --- Bashed Patch coverage ---
    _bp_masters: Set[str] = set()
    for pname, entry in entries.items():
        if pname.lower().startswith("bashed patch,"):
            for master in entry.masters:
                ml = master.lower()
                if ml not in BASE_GAME_PLUGINS:
                    _bp_masters.add(ml)
    if _bp_masters:
        for pname, entry in entries.items():
            if pname.lower() in _bp_masters:
                entry.patch_scents.add("PRE_PATCHED_BASHED")

    master_to_deps: Dict[str, List[str]] = {}
    for pname, entry in entries.items():
        for master in entry.masters:
            master_to_deps.setdefault(master.lower(), []).append(pname)
    for pname, entry in entries.items():
        entry.dependents = master_to_deps.get(pname.lower(), [])

    def _is_patched(e: "AuditEntry") -> bool:
        return e.is_patched and not e.has_new_records

    safe_set: Set[str] = set()
    changed = True
    while changed:
        changed = False
        for pname, entry in entries.items():
            if pname in safe_set or not _is_patched(entry):
                continue
            if all(dep in safe_set or dep not in entries for dep in entry.dependents):
                safe_set.add(pname)
                changed = True
    for pname, entry in entries.items():
        if pname in safe_set and entry.dependents:
            entry.transitively_safe = True
            entry.dependents = []
    return entries


def disable_plugins(game: "BaseGame", selected: "list[str]") -> "tuple[int, str]":
    """Remove the leading '*' from *selected* plugins in the active profile's
    plugins.txt (disabling them) and invalidate the read cache. Returns
    (disabled_count, message)."""
    profile = _read_active_profile(game)
    pdir = _profile_dir(game, profile)
    plugins_path = pdir / "plugins.txt"
    if not plugins_path.is_file():
        return 0, "plugins.txt not found — cannot disable."

    from Utils.plugins import _plugins_txt_encoding, _read_text_game_compat
    lines = _read_text_game_compat(plugins_path).splitlines()
    selected_lower = {n.lower() for n in selected}
    new_lines: list[str] = []
    disabled = 0
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("*"):
            name = stripped[1:]
            if name.lower() in selected_lower:
                new_lines.append(name)   # remove * to disable
                disabled += 1
                continue
        new_lines.append(line)
    text = "\n".join(new_lines) + "\n"
    plugins_path.write_text(text, encoding=_plugins_txt_encoding(text))
    from Utils.plugins import invalidate_plugins_cache
    invalidate_plugins_cache(plugins_path)
    return disabled, f"Disabled {disabled} plugin(s)."


def orphaned_ini_targets(entries: "Dict[str, AuditEntry]") -> "set[str]":
    """Lowercased names of patched plugins that cannot be disabled (their
    SkyGen INIs are candidates for cleanup)."""
    return {pname.lower() for pname, entry in entries.items()
            if not entry.can_disable and entry.is_patched}


def cleanup_orphaned_inis(game: "BaseGame", targets: "set[str]", *,
                          log_fn: "Callable[[str], None]" = lambda _m: None,
                          ) -> "tuple[int, int]":
    """Delete SkyGen-generated INIs (in the SkyGen BOS / SkyGen SkyPatcher
    output mods only) for plugins in *targets*. Returns (found, deleted)."""
    mods_path = _staging_path(game)
    if not mods_path or not mods_path.is_dir():
        log_fn("Cannot find mod staging path — aborting cleanup.")
        return 0, 0

    deleted = 0
    found = 0
    _for_re = re.compile(r"for\s+([^\s\r\n]+\.es[pml])", re.I)
    _extensions = (".esp", ".esm", ".esl")

    def _resolve_plugin(ini: Path, is_sp: bool = False) -> "Optional[str]":
        try:
            header = ini.read_text(encoding="utf-8", errors="ignore")[:512]
        except OSError:
            log_fn(f"  Cannot read {ini}")
            return None
        m = _for_re.search(header)
        if m:
            raw = m.group(1).lower()
            if raw in targets:
                return raw
        stem = ini.stem.lower()
        if is_sp:
            stem = re.sub(r"^[0-9a-f]+-", "", stem)
            if stem.endswith("_skygen"):
                stem = stem[:-7]
        else:
            for suffix in ("_skygen_swap", "_swap"):
                if stem.endswith(suffix):
                    stem = stem[:-len(suffix)]
                    break
        for ext in _extensions:
            candidate = stem + ext
            if candidate in targets:
                return candidate
        return stem + ".esp"

    for mod_name in ("SkyGen BOS", "SkyGen SkyPatcher"):
        mod_dir = mods_path / mod_name
        if not mod_dir.is_dir():
            continue
        bos_dir = mod_dir / "SKSE" / "Plugins" / "Data" / "Base Object Swapper"
        if bos_dir.is_dir():
            for ini in list(bos_dir.rglob("*.ini")):
                if not ini.is_file():
                    continue
                found += 1
                pkey = _resolve_plugin(ini, is_sp=False)
                if pkey and pkey in targets:
                    try:
                        ini.unlink(); deleted += 1
                        log_fn(f"  Deleted {ini.name}")
                    except OSError as exc:
                        log_fn(f"  Cannot delete {ini}: {exc}")
                else:
                    log_fn(f"  Skipped {ini.name} (plugin '{pkey}' not in blocked list)")
        for sp_name in ("SkyPatcher", "SkyPatcher2"):
            sp_dir = mod_dir / "SKSE" / "Plugins" / sp_name
            if not sp_dir.is_dir():
                continue
            for ini in list(sp_dir.rglob("*.ini")):
                if not ini.is_file():
                    continue
                found += 1
                pkey = _resolve_plugin(ini, is_sp=True)
                if pkey and pkey in targets:
                    try:
                        ini.unlink(); deleted += 1
                        log_fn(f"  Deleted {ini.name}")
                    except OSError as exc:
                        log_fn(f"  Cannot delete {ini}: {exc}")
                else:
                    log_fn(f"  Skipped {ini.name} (plugin '{pkey}' not in blocked list)")
            break
    return found, deleted
