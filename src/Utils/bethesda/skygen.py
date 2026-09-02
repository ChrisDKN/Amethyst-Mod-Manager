"""
GUI-neutral core of the SkyGen BOS/SkyPatcher patch generator.

Moved out of wizards/skygen.py (which imports customtkinter) so the Qt wizard
view can share it: the pure-Python plugin parser ("Frankensnoop"), the
load-order/profile/patch-index helpers, and standalone scan/generate/register functions
ported from the Tk wizard's methods.

The scan/generate functions take explicit callbacks (progress_fn, log_fn,
cancel_fn, is_selected) instead of touching widgets.
"""

from __future__ import annotations

import mmap
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Dict, List, Optional, Set, Tuple

if TYPE_CHECKING:
    from Games.base_game import BaseGame

# Frankensnoop - pure Python plugin parser (no MO2/PyQt6)

BASE_GAME_PLUGINS: Set[str] = {
    "Skyrim.esm", "Update.esm", "Dawnguard.esm",
    "HearthFires.esm", "Dragonborn.esm",
    "_ResourcePack.esl", "SkyrimVR.esm",
}

GLOBAL_IGNORE_PLUGINS: Set[str] = {
    "Skyrim.esm", "Update.esm", "Dawnguard.esm",
    "HearthFires.esm", "Dragonborn.esm",
    "_ResourcePack.esl", "SkyrimVR.esm",
}

# Signatures indicating pure framework/utility mods
GLOBAL_FRAMEWORK_SIGNATURES: Set[str] = {
    "SCPT", "SKSE", "SNDR", "SOUN", "CLMT", "WTHR", "MESG",
    "DLBR", "GMST", "KYWD",
}

# Protected authors (Bethesda/CC)
PROTECTED_AUTHORS: Set[str] = {
    "creationclub", "cc", "bethesda game studios",
    "bethesda", "mcarofano", "bnesmith", "rsalvatore",
}

# BOS Categories - must be defined before BOS_SIGS
BOS_CATEGORIES: Dict[str, Set[str]] = {
    "Tree": {"TREE"},
    "Furniture": {"FURN", "MSTT"},
    "Container": {"CONT"},
    "Light": {"LIGH"},
    "Misc": {"STAT", "ACTI", "DOOR", "FLOR"},
    "Body": {"ARMO", "ARMA", "NPC_", "RACE"},
    "Skin": {"ARMO", "ARMA", "NPC_", "RACE", "ASSET_SKIN", "ASSET_BODY"},
}

# BOS-eligible signatures - static objects BOS can swap
BOS_SIGS: Set[str] = set()
for _sigs in BOS_CATEGORIES.values():
    BOS_SIGS.update(_sigs)

# SkyPatcher-eligible signatures
SP_SIGS: Set[str] = {
    "NPC_", "WEAP", "ARMO", "AMMO", "ALCH", "BOOK", "LVLI", "LVLN", "FLST",
    "SPEL", "RACE", "ARMA", "HDPT", "HAIR", "FURN", "INGR", "KEYM",
}

# Signature → category mapping (BOS vs SkyPatcher)
SIGNATURE_TO_CATEGORIES: Dict[str, List[str]] = {
    "ARMO": ["Armor", "SkyPatcher"],
    "WEAP": ["Weapons", "SkyPatcher"],
    "AMMO": ["Ammo", "SkyPatcher"],
    "BOOK": ["Books", "SkyPatcher"],
    "ALCH": ["Alchemy", "SkyPatcher"],
    "INGR": ["Ingredients", "SkyPatcher"],
    "MISC": ["Misc", "BOS"],
    "STAT": ["Statics", "BOS"],
    "FURN": ["Furniture", "BOS"],
    "CONT": ["Containers", "BOS"],
    "LIGH": ["Lights", "BOS"],
    "DOOR": ["Doors", "BOS"],
    "ACTI": ["Activators", "BOS"],
    "TREE": ["Trees", "BOS"],
    "FLOR": ["Flora", "BOS"],
    "NPC_": ["NPCs", "SkyPatcher"],
    "SPEL": ["Spells", "SkyPatcher"],
    "RACE": ["Races", "SkyPatcher"],
    "LVLI": ["Leveled Items", "SkyPatcher"],
    "LVLN": ["Leveled NPCs", "SkyPatcher"],
    "FLST": ["Form Lists", "SkyPatcher"],
}

LOGIC_SIGS: Set[str] = {
    "QUST", "MGEF", "KYWD", "VMAD", "SCRP", "DLBR", "INFO", "DIAL",
}

# SkyPatcher example action per record signature
_SP_EXAMPLE_ACTION: Dict[str, str] = {
    "NPC_":  "SetEssential=0",
    "ARMO":  "SetWeight=0",
    "WEAP":  "SetWeight=0",
    "AMMO":  "SetWeight=0",
    "ALCH":  "SetWeight=0",
    "BOOK":  "SetWeight=0",
    "MISC":  "SetWeight=0",
    "INGR":  "SetWeight=0",
    "KEYM":  "SetWeight=0",
    "LVLI":  "ChangeChance=100",
    "LVLN":  "ChangeChance=100",
    "FLST":  "; AddToFormList=Skyrim.esm|<FormID>",
    "SPEL":  "; addSpell=Skyrim.esm|<FormID>",
    "RACE":  "; AddSpell=Skyrim.esm|<FormID>",
    "ARMA":  "; SetRace=Skyrim.esm|<FormID>",
    "HDPT":  "; SetRace=Skyrim.esm|<FormID>",
    "FURN":  "; SetKeyword=Skyrim.esm|<FormID>",
}
_SCENT_PATTERNS: Dict[str, re.Pattern] = {
    "SKSE":       re.compile(r"SKSE|skse|Script Extender", re.I),
    "DynDOLOD":   re.compile(r"DynDOLOD|dyndolod|LOD", re.I),
    "xEdit":      re.compile(r"SSEEdit|xEdit|TES5Edit|FO4Edit", re.I),
    "Synthesis":  re.compile(r"Synthesis|Mutagen", re.I),
    "Menu":       re.compile(r"SkyUI|AddItemMenu|RaceMenu|UIExtensions", re.I),
    "MCMHelper":  re.compile(r"MCMHelper|MCM", re.I),
    "PapyrusUtil": re.compile(r"PapyrusUtil", re.I),
    "ConsoleUtil": re.compile(r"ConsoleUtil", re.I),
    "JContainers": re.compile(r"JContainers", re.I),
    "AddressLib": re.compile(r"Address.*Library", re.I),
    "PO3":        re.compile(r"powerofthree|po3", re.I),
    "SPID":       re.compile(r"SPID|Spell.*Perk", re.I),
    "Framework":  re.compile(r"Framework|Base Object Swapper|Papyrus Extender|Address Library", re.I),
    "Engine":     re.compile(r"Engine Fixes|Bug Fixes|SSE Fixes", re.I),
    "BaseObjectSwapper": re.compile(r"BaseObjectSwapper|_SWAP\.ini", re.I),
    "SkyPatcher": re.compile(r"SkyPatcher", re.I),
    "AnimationFramework": re.compile(r"XPMSE|XP32|FNIS|Nemesis", re.I),
}
_BLACKLIST_AUTHORS = {
    "jaxonz", "expired6978", "meh321", "sheson", "doodlum",
    "powerofthree", "ryan-rsm-mckenzie", "aers", "nu_p_p",
    "tudoran", "towawot", "umgak", "versuchdrei", "xenius",
    "maskedrpgfan", "andrelo12", "krisv777", "edzio", "gopher",
    "banjobunny", "acro", "nemesis", "xpmse", "team xpmse", "groovtama",
    "mrowka", "colinswrath", "po3", "erkeil",
}


@dataclass
class PluginDNA:
    """Minimal plugin fingerprint - sufficient for the audit + patch gen."""
    plugin_name:   str
    signatures:    Set[str]
    author:        str
    masters:       List[str]
    folder_scents: Set[str] = field(default_factory=set)
    file_size:     int = 0
    dependents:    List[str] = field(default_factory=list)  # plugins that list this as a master

    @property
    def has_dependents(self) -> bool:
        return bool(self.dependents)

    @property
    def is_framework(self) -> bool:
        """True if this is a patcher/engine, not a content mod.
        
        Checks folder scents, author blacklist, masters-only plugins,
        and framework-only signatures.
        """
        if "BOS_FRAMEWORK" in self.folder_scents or "SKYPATCHER_FRAMEWORK" in self.folder_scents:
            return True
        al = self.author.lower()
        if any(a in al for a in _BLACKLIST_AUTHORS):
            return True
        if not self.signatures and self.masters:
            return True
    # Framework-only signatures
        if self.signatures and self.signatures <= GLOBAL_FRAMEWORK_SIGNATURES:
            return True
        return False

    @property
    def pre_patched_bos(self) -> bool:
        return "PRE_PATCHED_BOS" in self.folder_scents

    @property
    def pre_patched_sp(self) -> bool:
        return "PRE_PATCHED_SP" in self.folder_scents

    @property
    def pre_patched_bashed(self) -> bool:
        return "PRE_PATCHED_BASHED" in self.folder_scents

    @property
    def is_micro(self) -> bool:
        return "MICRO_FILE" in self.folder_scents

    @property
    def can_disable(self) -> bool:
        """True if patch exists AND no plugin depends on this as a master."""
        if self.has_dependents:
            return False
        return (self.pre_patched_bos or self.pre_patched_sp or self.pre_patched_bashed
                or "PRE_PATCHED_SPID" in self.folder_scents
                or "PRE_PATCHED_KID" in self.folder_scents
                or "PRE_PATCHED_DSD" in self.folder_scents
                or "PRE_PATCHED_OAR" in self.folder_scents
                or "PRE_PATCHED_SYNTHESIS" in self.folder_scents)

    @property
    def unsafe_reason(self) -> str:
        """Why this plugin can't be disabled. Empty if safe."""
        if self.has_dependents:
            deps = ", ".join(self.dependents[:5])
            extra = f" (+{len(self.dependents)-5} more)" if len(self.dependents) > 5 else ""
            return f"Required by: {deps}{extra}"
        return ""

    @property
    def disable_reason(self) -> str:
        reasons = []
        if self.pre_patched_bos:
            reasons.append("Base Object Swapper patch found")
        if self.pre_patched_sp:
            reasons.append("SkyPatcher INI patch found")
        if "PRE_PATCHED_SPID" in self.folder_scents:
            reasons.append("SPID distributor INI found")
        if "PRE_PATCHED_KID" in self.folder_scents:
            reasons.append("KID distributor INI found")
        if "PRE_PATCHED_DSD" in self.folder_scents:
            reasons.append("DSD JSON/YAML patch found")
        if "PRE_PATCHED_OAR" in self.folder_scents:
            reasons.append("OAR animation replacer found")
        if "PRE_PATCHED_SYNTHESIS" in self.folder_scents:
            reasons.append("Synthesis patcher output")
        if self.pre_patched_bashed:
            reasons.append("Bashed Patch merged")
        return " + ".join(reasons) if reasons else ""

    @property
    def bos_eligible(self) -> bool:
        """True if plugin has static object signatures (STAT, DOOR, FURN, etc.) eligible for BOS.
        
        Excludes Body/Skin unless pluginless body mod.
        """
        # Get static object signatures from BOS_CATEGORIES (exclude "Body"/"Skin" which are for pluginless mods)
        static_sigs = set()
        for cat_name, sigs in BOS_CATEGORIES.items():
            if cat_name in ("Body", "Skin"):
                continue  # pluginless body mods only
            static_sigs.update(sigs)

        has_static = bool(self.signatures & static_sigs)
        is_pluginless_body = "ASSET_SKIN" in self.folder_scents or "ASSET_BODY" in self.folder_scents

        if is_pluginless_body:
            return True

        return has_static

    @property
    def sp_eligible(self) -> bool:
        return bool(self.signatures & SP_SIGS)


def _scan_memoryview(mv) -> Tuple[Set[str], bool]:
    """Scan a bytes-like object for GRUP signatures."""
    signatures: Set[str] = set()
    if len(mv) < 24 or bytes(mv[:4]) != b"TES4":
        return signatures, False
    tes4_size = int.from_bytes(bytes(mv[4:8]), "little")
    offset = 24 + tes4_size
    if offset >= len(mv):
        return signatures, True
    file_len = len(mv)
    while offset < file_len - 24:
        if bytes(mv[offset:offset + 4]) == b"GRUP":
            rt = bytes(mv[offset + 8:offset + 12]).decode("ascii", errors="ignore")
            if len(rt) == 4 and rt.replace("_", "").isalnum():
                signatures.add(rt)
            grup_size = int.from_bytes(bytes(mv[offset + 4:offset + 8]), "little")
            if grup_size < 24:
                break
            offset += grup_size
        else:
            offset += 1
    return signatures, True


def _decompress_record(payload: bytes, uncompressed_size: int) -> Optional[bytes]:
    """Decompress a compressed TES4 record payload.

    Skyrim SE uses LZ4 block compression for most modern records; legacy LE
    plugins use zlib. Try each in turn. Returns None if all attempts fail -
    caller then falls back to FormID-only extraction.
    """
    if uncompressed_size <= 0 or not payload:
        return None
    try:
        import lz4.block as _lz4_block  # type: ignore
        try:
            out = _lz4_block.decompress(payload, uncompressed_size=uncompressed_size)
            if len(out) == uncompressed_size:
                return out
        except Exception:
            pass
    except ImportError:
        pass
    try:
        import lz4.frame as _lz4_frame  # type: ignore
        try:
            out = _lz4_frame.decompress(payload)
            if len(out) == uncompressed_size:
                return out
        except Exception:
            pass
    except ImportError:
        pass
    try:
        import zlib as _zlib
        out = _zlib.decompress(payload)
        if len(out) == uncompressed_size:
            return out
    except Exception:
        pass
    return None


def _read_edid_full(data: bytes, start: int, end: int) -> Tuple[str, str]:
    """Walk subrecords in ``data[start:end]`` and return (EDID, FULL).

    Handles XXXX extended-length subrecord prefix. Used for both compressed
    (decompressed buffer) and uncompressed records.
    """
    edid = ""
    full_name = ""
    sub = start
    while sub < end - 6:
        sub_tag = data[sub:sub + 4]
        if sub_tag == b"XXXX":
            sub_len = int.from_bytes(data[sub + 4:sub + 8], "little")
            sub += 8
            if sub + 4 > end:
                break
            sub_tag = data[sub:sub + 4]
            sub += 6
        else:
            sub_len = int.from_bytes(data[sub + 4:sub + 6], "little")
            sub += 6
        if sub + sub_len > end:
            break
        if sub_tag == b"EDID":
            raw = data[sub:sub + sub_len]
            edid = raw.split(b"\x00")[0].decode("ascii", errors="ignore").strip()
        elif sub_tag == b"FULL":
            raw = data[sub:sub + sub_len]
            full_name = raw.split(b"\x00")[0].decode("utf-8", errors="ignore").strip()
        sub += sub_len
    return edid, full_name


def _extract_records(path: Path, wanted_sigs: Set[str]) -> List[Dict]:
    """Extract FormIDs for records matching *wanted_sigs*.

    Returns [{form_id, local_id, signature, editor_id, name}].
    Walks GRUP blocks only (skips TES4 header). Safe from bg thread.
    Compressed records are decompressed (LZ4 block → LZ4 frame → zlib) so
    EDID/FULL are still recovered; if decompression fails the record is
    still emitted with just its FormID.
    """
    records: List[Dict] = []
    try:
        data = path.read_bytes()
    except OSError:
        return records

    if len(data) < 24 or data[:4] != b"TES4":
        return records

    tes4_size = int.from_bytes(data[4:8], "little")
    offset = 24 + tes4_size
    file_len = len(data)
    wanted_bytes = {s.encode("ascii") for s in wanted_sigs}

    while offset < file_len - 24:
        tag = data[offset:offset + 4]
        if tag == b"GRUP":
            grup_size = int.from_bytes(data[offset + 4:offset + 8], "little")
            grup_type_sig = data[offset + 8:offset + 12]
            if grup_size < 24:
                offset += 1
                continue
            if grup_type_sig in wanted_bytes:
                # Walk records inside this GRUP
                rec_offset = offset + 24
                grup_end = offset + grup_size
                sig_str = grup_type_sig.decode("ascii", errors="ignore")
                while rec_offset < grup_end - 24:
                    rec_tag = data[rec_offset:rec_offset + 4]
                    rec_size = int.from_bytes(data[rec_offset + 4:rec_offset + 8], "little")
                    rec_flags = int.from_bytes(data[rec_offset + 8:rec_offset + 12], "little")
                    form_id_raw = int.from_bytes(data[rec_offset + 12:rec_offset + 16], "little")
                    rec_start = rec_offset + 24
                    rec_end = rec_start + rec_size
                    if rec_tag == b"GRUP":
                        # Nested GRUP (cell children etc.) - skip
                        nested_size = int.from_bytes(data[rec_offset + 4:rec_offset + 8], "little")
                        rec_offset += max(nested_size, 24)
                        continue
                    if rec_end > file_len or rec_size > 10 * 1024 * 1024:
                        rec_offset += 24
                        continue
                    is_compressed = bool(rec_flags & 0x00040000)
                    edid = ""
                    full_name = ""
                    if rec_size > 0:
                        if is_compressed:
                            # Compressed records start with a 4-byte
                            # uncompressed size, then the compressed payload.
                            if rec_size >= 4:
                                uncomp_size = int.from_bytes(
                                    data[rec_start:rec_start + 4], "little"
                                )
                                payload = data[rec_start + 4:rec_end]
                                buf = _decompress_record(payload, uncomp_size)
                                if buf is not None:
                                    edid, full_name = _read_edid_full(
                                        buf, 0, len(buf)
                                    )
                        else:
                            edid, full_name = _read_edid_full(
                                data, rec_start, rec_end
                            )
                    if form_id_raw:
                        fid_str = f"{form_id_raw:08X}"
                        local_id = fid_str[-6:]
                        records.append({
                            "form_id": fid_str,
                            "local_id": local_id,
                            "editor_id": edid,
                            "name": full_name,
                            "signature": sig_str,
                        })
                    rec_offset = rec_end
            offset += grup_size
        else:
            offset += 1
    return records



def _extract_signatures(path: Path) -> Set[str]:
    try:
        with open(path, "rb") as f:
            try:
                with mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as mm:
                    sigs, ok = _scan_memoryview(mm)
                    if ok:
                        return sigs
            except (OSError, ValueError, mmap.error):
                pass
            f.seek(0)
            data = f.read()
            sigs, _ = _scan_memoryview(memoryview(data))
            return sigs
    except Exception:
        return set()


def _parse_header(path: Path) -> Tuple[str, List[str]]:
    """Return (author, [masters])."""
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


def _detect_scents(plugin_path: Path, mod_folder: Optional[Path]) -> Set[str]:
    scents: Set[str] = set()
    if mod_folder and mod_folder.is_dir():
        stem = plugin_path.stem
        name = plugin_path.name
        # Search mod root + FOMOD Data/ subfolder
        search_roots = [mod_folder]
        data_sub = mod_folder / "Data"
        if data_sub.is_dir():
            search_roots.append(data_sub)

        for search_root in search_roots:
            # BOS: <stem>_SWAP.ini anywhere in mod
            try:
                for f in search_root.rglob("*.ini"):
                    if f.is_file() and f.name.lower() == f"{stem.lower()}_swap.ini":
                        scents.add("PRE_PATCHED_BOS")
                        scents.add("BOS_FRAMEWORK")
                        break
            except OSError:
                pass
            # Also check SKSE/Plugins/Data/Base Object Swapper/
            bos_dir = search_root / "SKSE" / "Plugins" / "Data" / "Base Object Swapper"
            if bos_dir.is_dir():
                try:
                    for ini in bos_dir.rglob("*.ini"):
                        txt = ini.read_text(encoding="utf-8", errors="ignore")
                        if name in txt or stem in txt:
                            scents.add("PRE_PATCHED_BOS")
                            scents.add("BOS_FRAMEWORK")
                            break
                except Exception:
                    pass

        # SkyPatcher: SKSE/Plugins/SkyPatcher/ or SkyPatcher2/
        for sp_dir_name in ("SkyPatcher", "SkyPatcher2"):
            sp_dir = mod_folder / "SKSE" / "Plugins" / sp_dir_name
            if sp_dir.is_dir():
                scents.add("SKYPATCHER_FRAMEWORK")
                try:
                    for ini in sp_dir.rglob("*.ini"):
                        chunk = ini.read_text(encoding="utf-8", errors="ignore")[:2048]
                        if name in chunk or stem in chunk:
                            scents.add("PRE_PATCHED_SP")
                            break
                except Exception:
                    pass
                break

        # Additional patch frameworks
        for search_root in search_roots:
            # SPID: <stem>_DISTR.ini
            try:
                for f in search_root.rglob("*.ini"):
                    if f.is_file() and f.name.lower() == f"{stem.lower()}_distr.ini":
                        scents.add("PRE_PATCHED_SPID")
                        break
            except OSError:
                pass
            # KID: <stem>_KID.ini
            try:
                for f in search_root.rglob("*.ini"):
                    if f.is_file() and f.name.lower() == f"{stem.lower()}_kid.ini":
                        scents.add("PRE_PATCHED_KID")
                        break
            except OSError:
                pass
            # DSD: <stem>_DSD.json / _DSD.yaml
            try:
                for f in search_root.rglob("*.json"):
                    if f.is_file() and f.name.lower() == f"{stem.lower()}_dsd.json":
                        scents.add("PRE_PATCHED_DSD")
                        break
            except OSError:
                pass
            try:
                for f in search_root.rglob("*.yaml"):
                    if f.is_file() and f.name.lower() == f"{stem.lower()}_dsd.yaml":
                        scents.add("PRE_PATCHED_DSD")
                        break
            except OSError:
                pass

        # OAR: SKSE/Plugins/OpenAnimationReplacer/
        oar_dir = mod_folder / "SKSE" / "Plugins" / "OpenAnimationReplacer"
        if oar_dir.is_dir():
            scents.add("PRE_PATCHED_OAR")

        # Synthesis: plugin named Synthesis.esp
        if stem.lower() == "synthesis":
            scents.add("PRE_PATCHED_SYNTHESIS")

        # Micro file: < 2 KB ESL stub
        try:
            if plugin_path.exists() and plugin_path.stat().st_size < 2 * 1024:
                scents.add("MICRO_FILE")
        except Exception:
            pass
    # Name-pattern scents
    for sname, pattern in _SCENT_PATTERNS.items():
        if pattern.search(plugin_path.name):
            scents.add(sname)
    return scents


def _sniff_plugin(plugin_path: Path, mod_folder: Optional[Path]) -> PluginDNA:
    sigs = _extract_signatures(plugin_path)
    author, masters = _parse_header(plugin_path)
    scents = _detect_scents(plugin_path, mod_folder)
    return PluginDNA(
        plugin_name=plugin_path.name,
        signatures=sigs,
        author=author,
        masters=masters,
        folder_scents=scents,
        file_size=plugin_path.stat().st_size if plugin_path.exists() else 0,
    )


# Path helpers

def _read_active_profile(game: "BaseGame") -> str:
    """Current profile name: _active_profile_dir → last active → default."""
    # 1. Live profile dir set by Amethyst
    active_dir = getattr(game, "_active_profile_dir", None)
    if active_dir is not None:
        name = Path(active_dir).name
        if name:
            return name

    # 2. Persisted last active
    try:
        last = game.get_last_active_profile()
        if last and last != "default":
            # Verify the profile dir actually exists
            candidate = game.get_profile_root() / "profiles" / last
            if candidate.is_dir():
                return last
    except Exception:
        pass

    # 3. First non-default with loadorder.txt, else "default"
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
    """Return ordered list of *active* plugin names for the given profile."""
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
                # Older format without star prefix - treat all as active
                active_set.add(line)

    if not full_order:
        return list(active_set)

    # Preserve order, filter to active only (base masters always included)
    return [p for p in full_order if p in BASE_GAME_PLUGINS or p in active_set]


def _find_plugin_file(plugin_name: str, mods_path: Path, data_path: Optional[Path]) -> Optional[Path]:
    """3-tier search: mods/ → mods/<n>/Data/ → game Data/."""
    if mods_path.is_dir():
        for mod_dir in mods_path.iterdir():
            if not mod_dir.is_dir():
                continue
            cand = mod_dir / plugin_name
            if cand.is_file():
                return cand
            cand2 = mod_dir / "Data" / plugin_name
            if cand2.is_file():
                return cand2
    if data_path and data_path.is_dir():
        cand = data_path / plugin_name
        if cand.is_file():
            return cand
    return None


def _build_patch_index(mods_path: Path) -> Tuple[Set[str], Set[str]]:
    """Scan staging directory for existing patches.
    
    Returns (bos_patched, sp_patched) - lower-case plugin name sets.
    Catches patches in different mods than the target plugin.
    """
    bos_patched: Set[str] = set()
    sp_patched: Set[str] = set()

    if not mods_path or not mods_path.is_dir():
        return bos_patched, sp_patched

    _for_re = re.compile(r"for\s+([^\s\r\n]+\.es[pml])", re.I)

    try:
        for mod_dir in mods_path.iterdir():
            if not mod_dir.is_dir():
                continue

            # --- BOS: any *_SWAP.ini anywhere in the mod tree ---
            bos_dir = mod_dir / "SKSE" / "Plugins" / "Data" / "Base Object Swapper"
            if bos_dir.is_dir():
                try:
                    for ini in bos_dir.rglob("*.ini"):
                        try:
                            header = ini.read_text(encoding="utf-8", errors="ignore")[:512]
                        except OSError:
                            continue
                        m = _for_re.search(header)
                        if m:
                            bos_patched.add(m.group(1).lower())
                        else:
                            # Fall back: strip _SkyGen_SWAP / _SWAP suffix from filename
                            stem = ini.stem.lower()
                            for suffix in ("_skygen_swap", "_swap"):
                                if stem.endswith(suffix):
                                    stem = stem[: -len(suffix)]
                                    break
                            # stem could map to .esp/.esm/.esl - try all
                            for ext in (".esp", ".esm", ".esl"):
                                bos_patched.add(stem + ext)
                except OSError:
                    pass

            # Also catch bare <stem>_SWAP.ini files at mod root (e.g. hand-authored)
            try:
                for ini in mod_dir.rglob("*_swap.ini"):
                    stem = ini.stem.lower()
                    if stem.endswith("_swap"):
                        stem = stem[:-5]
                    for ext in (".esp", ".esm", ".esl"):
                        bos_patched.add(stem + ext)
            except OSError:
                pass

            # --- SkyPatcher: SKSE/Plugins/SkyPatcher[2]/ ---
            for sp_name in ("SkyPatcher", "SkyPatcher2"):
                sp_dir = mod_dir / "SKSE" / "Plugins" / sp_name
                if sp_dir.is_dir():
                    try:
                        for ini in sp_dir.rglob("*.ini"):
                            try:
                                header = ini.read_text(encoding="utf-8", errors="ignore")[:512]
                            except OSError:
                                continue
                            m = _for_re.search(header)
                            if m:
                                sp_patched.add(m.group(1).lower())
                            else:
                                # Strip LO prefix (e.g. "1F-") and _SkyGen suffix
                                stem = ini.stem.lower()
                                stem = re.sub(r"^[0-9a-f]+-", "", stem)
                                if stem.endswith("_skygen"):
                                    stem = stem[:-7]
                                for ext in (".esp", ".esm", ".esl"):
                                    sp_patched.add(stem + ext)
                    except OSError:
                        pass
                    break  # found a SP dir, no need to check SP2
    except OSError:
        pass

    return bos_patched, sp_patched


def _find_mod_folder(plugin_path: Path, mods_path: Path) -> Optional[Path]:
    """Resolve the mod staging folder that owns this plugin."""
    try:
        rel = plugin_path.relative_to(mods_path)
        # rel is <ModFolder>/... so first part is the mod folder
        return mods_path / rel.parts[0]
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Scan / generate / register (ported from the Tk wizard methods)
# ---------------------------------------------------------------------------

def _staging_path(game: "BaseGame") -> Path:
    getter = (getattr(game, "get_effective_mod_staging_path", None)
              or game.get_mod_staging_path)
    return getter()


def scan_load_order(game: "BaseGame", *,
                    progress_fn: "Callable[[float, str, int, int], None]" = lambda *a: None,
                    log_fn: "Callable[[str], None]" = lambda _m: None,
                    cancel_fn: "Callable[[], bool]" = lambda: False,
                    ) -> "tuple[Dict[str, PluginDNA], Dict[str, int], str] | None":
    """Scan the active profile's load order, returning
    (dna_map, lo_index_map, summary) or None when cancelled / no plugins.

    Blocking; call from a worker thread. progress_fn(frac, name, idx, total)
    is called periodically; cancel_fn() is polled between plugins.
    """
    profile = _read_active_profile(game)
    log_fn(f"Active profile: '{profile}'")
    lo = _read_loadorder(game, profile)
    if not lo:
        return None
    mods_path = _staging_path(game)
    game_path = game.get_game_path()
    data_path = game_path / "Data" if game_path else None

    total = len(lo)
    dna_map: Dict[str, PluginDNA] = {}

    log_fn("Building cross-mod patch index…")
    bos_index, sp_index = _build_patch_index(mods_path)
    log_fn(f"Patch index: {len(bos_index)} BOS targets, {len(sp_index)} SP targets")

    for i, plugin_name in enumerate(lo):
        if cancel_fn():
            return None
        if plugin_name in GLOBAL_IGNORE_PLUGINS:
            continue
        plugin_path = _find_plugin_file(plugin_name, mods_path, data_path)
        if not plugin_path:
            continue
        mod_folder = _find_mod_folder(plugin_path, mods_path)
        try:
            dna = _sniff_plugin(plugin_path, mod_folder)
        except Exception as exc:
            log_fn(f"Sniff failed for {plugin_name}: {exc}")
            continue
        pkey = plugin_name.lower()
        if pkey in bos_index:
            dna.folder_scents.add("PRE_PATCHED_BOS")
        if pkey in sp_index:
            dna.folder_scents.add("PRE_PATCHED_SP")
        dna_map[plugin_name] = dna
        if i % 50 == 0 or i == total - 1:
            frac = (i + 1) / total if total else 0
            progress_fn(frac, plugin_name, i, total)

    lo_index_map = {name: i for i, name in enumerate(lo)}

    # --- Bashed Patch coverage ---
    _bp_masters: Set[str] = set()
    for pname, dna in dna_map.items():
        if pname.lower().startswith("bashed patch,"):
            for master in dna.masters:
                ml = master.lower()
                if ml not in BASE_GAME_PLUGINS:
                    _bp_masters.add(ml)
    if _bp_masters:
        for pname, dna in dna_map.items():
            if pname.lower() in _bp_masters:
                dna.folder_scents.add("PRE_PATCHED_BASHED")

    # Reverse dependency map - flag masters of other active plugins.
    master_to_dependents: Dict[str, List[str]] = {}
    for pname, dna in dna_map.items():
        for master in dna.masters:
            master_to_dependents.setdefault(master.lower(), []).append(pname)
    for pname, dna in dna_map.items():
        deps = master_to_dependents.get(pname.lower(), [])
        if deps:
            dna.dependents = deps

    # Transitive safe-to-disable resolution.
    def _is_patched(d: "PluginDNA") -> bool:
        return (d.pre_patched_bos or d.pre_patched_sp or d.pre_patched_bashed
                or "PRE_PATCHED_SPID" in d.folder_scents
                or "PRE_PATCHED_KID" in d.folder_scents
                or "PRE_PATCHED_DSD" in d.folder_scents
                or "PRE_PATCHED_OAR" in d.folder_scents
                or "PRE_PATCHED_SYNTHESIS" in d.folder_scents)

    safe_set: Set[str] = set()
    changed = True
    while changed:
        changed = False
        for pname, dna in dna_map.items():
            if pname in safe_set or not _is_patched(dna):
                continue
            if all(dep in safe_set or dep not in dna_map for dep in dna.dependents):
                safe_set.add(pname)
                changed = True
    for pname, dna in dna_map.items():
        if pname in safe_set and dna.dependents:
            dna.folder_scents.add("TRANSITIVELY_SAFE")
            dna.dependents = []

    bos_eligible = sum(1 for d in dna_map.values() if d.bos_eligible and not d.is_framework)
    sp_eligible = sum(1 for d in dna_map.values() if d.sp_eligible and not d.is_framework)
    summary = (
        f"  Plugins scanned:                {len(dna_map)}\n"
        f"  BOS-eligible for new patches:   {bos_eligible}\n"
        f"  SkyPatcher-eligible:            {sp_eligible}"
    )
    return dna_map, lo_index_map, summary


def generate_patches(game: "BaseGame", mode: str,
                     dna_map: "Dict[str, PluginDNA]",
                     lo_index_map: "Dict[str, int]", *,
                     is_selected: "Callable[[str], bool]" = lambda _n: True,
                     cancel_fn: "Callable[[], bool]" = lambda: False,
                     log_fn: "Callable[[str], None]" = lambda _m: None,
                     ) -> "tuple[str, Path, int]":
    """Generate BOS or SkyPatcher INIs for the selected plugins and register
    the output as a managed mod. Returns (mod_name, out_dir, files_written).
    mode is "BOS" or "SkyPatcher". Blocking; call from a worker thread."""
    out_name = "SkyGen BOS" if mode == "BOS" else "SkyGen SkyPatcher"
    mods_path = _staging_path(game)
    out_dir = mods_path / out_name
    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    game_path = game.get_game_path()
    data_path = game_path / "Data" if game_path else None

    if mode == "BOS":
        written = _gen_bos(out_dir, dna_map, mods_path, data_path,
                           is_selected, cancel_fn, log_fn)
    else:
        written = _gen_skypatcher(out_dir, dna_map, lo_index_map, mods_path,
                                  data_path, is_selected, cancel_fn, log_fn)

    register_output_mod(game, out_name, out_dir, log_fn=log_fn)
    log_fn(f"Patch generation complete. Output: {out_dir}")
    return out_name, out_dir, written


def register_output_mod(game: "BaseGame", mod_name: str, mod_dir: Path, *,
                        log_fn: "Callable[[str], None]" = lambda _m: None) -> None:
    """Write meta.ini + prepend the mod to the active profile's modlist so
    Amethyst treats the output as a managed, enabled mod."""
    from Nexus.nexus_meta import NexusModMeta, write_meta
    from Utils.mods.modlist import prepend_mod

    meta = NexusModMeta(mod_name=mod_name, installation_file="SkyGen",
                        root_folder=False)
    write_meta(mod_dir / "meta.ini", meta)

    try:
        profile = _read_active_profile(game)
    except Exception:
        profile = "default"
    modlist_path = _profile_dir(game, profile) / "modlist.txt"
    if not modlist_path.is_file():
        modlist_path = game.get_profile_root() / "modlist.txt"
    prepend_mod(modlist_path, mod_name, enabled=True)
    log_fn(f"Registered '{mod_name}' in modlist as enabled mod.")


def _gen_bos(out_dir: Path, dna_map, mods_path, data_path, is_selected,
             cancel_fn, log_fn) -> int:
    bos_dir = out_dir / "SKSE" / "Plugins" / "Data" / "Base Object Swapper"
    bos_dir.mkdir(parents=True, exist_ok=True)
    written = 0
    for plugin_name, dna in dna_map.items():
        if cancel_fn():
            break
        if not dna.bos_eligible or dna.is_framework:
            continue
        if not is_selected(plugin_name):
            continue
        bos_sigs = dna.signatures & BOS_SIGS
        if not bos_sigs:
            continue
        plugin_path = _find_plugin_file(plugin_name, mods_path, data_path)
        records = _extract_records(plugin_path, bos_sigs) if plugin_path else []

        stem = Path(plugin_name).stem
        out_ini = bos_dir / f"{stem}_SkyGen_SWAP.ini"
        lines = [
            f"; Auto-generated by SkyGen (Amethyst) for {plugin_name}",
            f"; Record types: {', '.join(sorted(bos_sigs))}",
            "; Format: sourceObject|replacementObject|NONE|chanceR(percent)",
            "; Edit the right side and chance values for your setup.",
            "[Forms]",
            "",
        ]
        if records:
            for rec in records:
                label = rec["editor_id"] or rec["name"] or rec["local_id"]
                orig = f"0x{rec['local_id']}~{plugin_name}"
                placeholder = f"0x{rec['local_id']}~<ReplacementPlugin>"
                entry = f"{orig}|{placeholder}|NONE|chanceR(100)"
                lines.append(f"; [{rec['signature']}] {label}")
                lines.append(entry)
            lines.append("")
        else:
            lines += [
                f"; Could not extract records from {plugin_name}.",
                "; Format: objectName|replacementName|NONE|chanceR(100)",
            ]
        out_ini.write_text("\n".join(lines), encoding="utf-8")
        written += 1
        log_fn(f"BOS: {out_ini.name} ({len(records)} records)")
    log_fn(f"BOS done: {written} INIs written to {bos_dir}")
    return written


def _gen_skypatcher(out_dir: Path, dna_map, lo_map, mods_path, data_path,
                    is_selected, cancel_fn, log_fn) -> int:
    from collections import defaultdict
    sp_dir = out_dir / "SKSE" / "Plugins" / "SkyPatcher"
    sp_dir.mkdir(parents=True, exist_ok=True)
    written = 0
    for plugin_name, dna in dna_map.items():
        if cancel_fn():
            break
        if not dna.sp_eligible or dna.is_framework:
            continue
        if not is_selected(plugin_name):
            continue
        sp_sigs = dna.signatures & SP_SIGS
        if not sp_sigs:
            continue
        plugin_path = _find_plugin_file(plugin_name, mods_path, data_path)
        records = _extract_records(plugin_path, sp_sigs) if plugin_path else []

        lo_idx = lo_map.get(plugin_name, 999)
        if plugin_name.lower().endswith(".esl"):
            prefix = f"FE{lo_idx:03X}"
        else:
            prefix = f"{lo_idx:02X}"
        stem = Path(plugin_name).stem
        out_ini = sp_dir / f"{prefix}-{stem}_SkyGen.ini"

        lines = [
            f"; Auto-generated by SkyGen (Amethyst) for {plugin_name}",
            f"; Record types: {', '.join(sorted(sp_sigs))}",
            "; Edit the right-hand value of each Set<Field> line to apply your change.",
            "; SkyPatcher filterByFormID uses full 8-char FormID (with load order).",
            "",
        ]
        by_sig: dict = defaultdict(list)
        for rec in records:
            by_sig[rec["signature"]].append(rec)
        if by_sig:
            for sig in sorted(by_sig.keys()):
                lines.append(f"; --- {sig} records ---")
                for rec in by_sig[sig]:
                    label = rec["editor_id"] or rec["name"] or rec["local_id"]
                    lines.append(f"; {label}")
                    form_id = rec["form_id"]
                    lines.append(f"filterByFormID={plugin_name}|{form_id}")
                    example = _SP_EXAMPLE_ACTION.get(sig, "; Set<Field> = <Value>")
                    lines.append(f"; {example}")
                    lines.append("")
        else:
            for sig in sorted(sp_sigs):
                lines += [
                    f"; [{sig}] - could not extract FormIDs from {plugin_name}",
                    f"; filterByFormID={plugin_name}|<FormID>",
                    f"; {_SP_EXAMPLE_ACTION.get(sig, 'Set<Field> = <Value>')}",
                    "",
                ]
        out_ini.write_text("\n".join(lines), encoding="utf-8")
        written += 1
        log_fn(f"SP: {out_ini.name} ({len(records)} records)")
    log_fn(f"SkyPatcher done: {written} INIs written to {sp_dir}")
    return written
