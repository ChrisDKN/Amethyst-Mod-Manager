"""Toolkit-neutral core for the Downloads tab - archive scanning, installed-mod
detection, size/ext helpers, and the filter pass. Ported from the pure-Python
parts of gui/downloads_panel.py so the Qt Downloads tab reuses the exact logic.
Pure stdlib + Utils.*/Nexus.* - no GUI toolkit.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from Utils.download_locations import (
    is_default_downloads_disabled, is_cache_default_disabled,
    get_default_downloads_dir, load_extra_download_locations,
)

_ARCHIVE_EXTS = {".zip", ".7z", ".rar", ".tar", ".tar.gz", ".tar.bz2", ".tar.xz",
                 ".dazip", ".override", ".fomod"}


def is_archive(name: str) -> bool:
    low = name.lower()
    return any(low.endswith(ext) for ext in _ARCHIVE_EXTS)


def file_extension(name: str) -> str:
    """Lowercase extension, treating compound .tar.* together."""
    low = name.lower()
    for ext in (".tar.gz", ".tar.bz2", ".tar.xz"):
        if low.endswith(ext):
            return ext
    i = low.rfind(".")
    return low[i:] if i >= 0 else ""


def fmt_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f} {unit}" if unit != "B" else f"{n} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


# ---------------------------------------------------------------------------
# Installed-mod index (Install vs Reinstall)
# ---------------------------------------------------------------------------
def parse_archive_mod_file_ids(name: str) -> Optional[tuple[int, int]]:
    """(mod_id, file_id) parsed from a Nexus-style archive filename, or None.
    Nexus downloads end ``-<mod_id>-<version_parts>-<file_id>`` (numeric)."""
    try:
        from Nexus.nexus_meta import parse_nexus_filename
    except Exception:
        return None
    stem = name
    for ext in (".tar.gz", ".tar.bz2", ".tar.xz"):
        if stem.lower().endswith(ext):
            stem = stem[: -len(ext)]
            break
    else:
        i = stem.rfind(".")
        if i >= 0:
            stem = stem[:i]
    info = parse_nexus_filename(stem)
    if info is None or not info.version_parts:
        return None
    return (int(info.mod_id), int(info.version_parts[-1]))


@dataclass
class InstalledIndex:
    """Snapshot of installed mods, to mark Downloads-tab archives as installed.
    `names` = exact installationFile match; `mod_file_ids` = (mod_id,file_id)
    fallback (collections store the canonical Nexus name, not the download name)."""
    names: set[str] = field(default_factory=set)
    mod_file_ids: set[tuple[int, int]] = field(default_factory=set)

    def is_archive_installed(self, archive_name: str) -> bool:
        if archive_name in self.names:
            return True
        ids = parse_archive_mod_file_ids(archive_name)
        return ids is not None and ids in self.mod_file_ids


def build_installed_index(game) -> InstalledIndex:
    """Walk the game's staging, read each meta.ini, collect installation_file +
    (mod_id, file_id). Port of Tk _get_installed_filenames (game, not panel)."""
    idx = InstalledIndex()
    try:
        from Nexus.nexus_meta import read_meta
        if game is None or not game.is_configured():
            return idx
        staging = game.get_effective_mod_staging_path()
        if not staging or not Path(staging).is_dir():
            return idx
        for folder in Path(staging).iterdir():
            meta_path = folder / "meta.ini"
            if not meta_path.is_file():
                continue
            try:
                m = read_meta(meta_path)
            except Exception:
                continue
            if m.installation_file:
                idx.names.add(m.installation_file)
            if m.mod_id and m.file_id:
                idx.mod_file_ids.add((int(m.mod_id), int(m.file_id)))
    except Exception:
        return idx
    return idx


# ---------------------------------------------------------------------------
# Scanning
# ---------------------------------------------------------------------------
@dataclass
class DownloadEntry:
    """One row - an archive or a synthetic section header (group by source dir)."""
    is_section_header: bool = False
    section_name: str = ""
    path: Optional[Path] = None
    size_str: str = ""
    size: int = 0
    src_dir: Optional[Path] = None


def get_scan_dirs(game_name: Optional[str]) -> list[Path]:
    """Default Downloads + the active game's download_cache + user extras,
    honouring the disable toggles, de-duplicated by resolved path. Port of Tk
    _get_scan_dirs (game_name instead of reading the topbar)."""
    dirs: list[Path] = []
    seen: set[Path] = set()

    def _add(p: Path):
        try:
            key = p.resolve()
        except OSError:
            key = p
        if key not in seen:
            dirs.append(p)
            seen.add(key)

    if not is_default_downloads_disabled():
        _add(get_default_downloads_dir())
    if not is_cache_default_disabled() and game_name:
        try:
            from Utils.config_paths import get_download_cache_dir_for_game
            cache = get_download_cache_dir_for_game(game_name)
            if cache is not None:
                _add(cache)
        except Exception:
            pass
    for p in load_extra_download_locations():
        path = Path(p).expanduser()
        try:
            rp = path.resolve()
        except OSError:
            rp = path
        if rp.is_dir() and rp not in seen:
            dirs.append(rp)
            seen.add(rp)
    return dirs


def section_label_for_dir(dl_dir: Path, game_name: Optional[str]) -> str:
    """User-facing label for a scan dir's section header / location filter."""
    def _res(p):
        try:
            return p.resolve()
        except OSError:
            return p
    dl_key = _res(dl_dir)
    if dl_key == _res(get_default_downloads_dir()):
        return "Downloads"
    if game_name and not is_cache_default_disabled():
        try:
            from Utils.config_paths import get_download_cache_dir_for_game
            cache = get_download_cache_dir_for_game(game_name)
            if cache is not None and dl_key == _res(cache):
                return "Mod Manager Cache"
        except Exception:
            pass
    return f"{dl_dir}"


def _is_default_downloads_dir(dl_dir: Path) -> bool:
    """True if dl_dir is the system default Downloads folder (the only location
    that sorts newest-first; cache/extras stay alphabetical)."""
    def _res(p):
        try:
            return p.resolve()
        except OSError:
            return p
    return _res(dl_dir) == _res(get_default_downloads_dir())


def scan_download_dirs(game_name: Optional[str]) -> list[DownloadEntry]:
    """Scan all configured locations for archives → [DownloadEntry] with one
    section header per source dir. The default Downloads location is sorted
    newest-first (by mtime); the Mod Manager Cache and extra locations stay
    alphabetical."""
    entries: list[DownloadEntry] = []
    for dl_dir in get_scan_dirs(game_name):
        bucket: list[tuple[Path, float, int]] = []
        if dl_dir.is_dir():
            try:
                for entry in dl_dir.iterdir():
                    if entry.is_file() and is_archive(entry.name):
                        try:
                            st = entry.stat()
                        except OSError:
                            continue
                        if st.st_size == 0:
                            # In-progress browser downloads (e.g. Firefox) create a
                            # 0-byte placeholder with the final name next to the
                            # .part file - hide it until the download completes.
                            continue
                        bucket.append((entry, st.st_mtime, st.st_size))
            except OSError:
                pass
        if _is_default_downloads_dir(dl_dir):
            # Downloads: newest first (by modified time), so freshly downloaded
            # archives appear at the top.
            bucket.sort(key=lambda t: t[1], reverse=True)
        else:
            # Cache / extra locations: alphabetical (case-insensitive).
            bucket.sort(key=lambda t: t[0].name.casefold())
        # A section header is emitted for EVERY scan dir (even empty ones) so the
        # cache / extra locations always show - Tk parity.
        entries.append(DownloadEntry(
            is_section_header=True,
            section_name=section_label_for_dir(dl_dir, game_name),
            src_dir=dl_dir))
        for p, _mt, sz in bucket:
            entries.append(DownloadEntry(
                path=p, size_str=fmt_size(sz), size=sz, src_dir=dl_dir))
    return entries


def filetype_counts(entries: list[DownloadEntry]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for e in entries:
        if e.is_section_header or e.path is None:
            continue
        ext = file_extension(e.path.name)
        counts[ext] = counts.get(ext, 0) + 1
    return counts


def location_options(entries: list[DownloadEntry],
                     game_name: Optional[str]) -> list[tuple[str, str, int]]:
    """(resolved_dir_key, label, count) per section, for the location filter."""
    out: list[tuple[str, str, int]] = []
    i, n = 0, len(entries)
    while i < n:
        e = entries[i]
        if e.is_section_header:
            j = i + 1
            cnt = 0
            while j < n and not entries[j].is_section_header:
                cnt += 1
                j += 1
            key = _resolved(e.src_dir)
            out.append((key, e.section_name, cnt))
            i = j
        else:
            i += 1
    return out


def _resolved(p: Optional[Path]) -> str:
    if p is None:
        return ""
    try:
        return str(p.resolve())
    except OSError:
        return str(p)


# ---------------------------------------------------------------------------
# Filtering (port of Tk _apply_filters)
# ---------------------------------------------------------------------------
def filter_entries(entries: list[DownloadEntry], installed: InstalledIndex, *,
                   only_installed: int = 0, only_not_installed: int = 0,
                   locations: frozenset | None = None,
                   locations_exclude: frozenset | None = None,
                   filetypes: frozenset | None = None,
                   filetypes_exclude: frozenset | None = None,
                   search: str = "") -> list[DownloadEntry]:
    """Apply (AND) ext / location / status / search filters; drop section headers
    whose group ends up empty. only_installed/only_not_installed are tri-state
    (0 off, 1 include, 2 exclude)."""
    locations = locations or frozenset()
    locations_exclude = locations_exclude or frozenset()
    filetypes = filetypes or frozenset()
    filetypes_exclude = filetypes_exclude or frozenset()
    query = (search or "").casefold()
    any_active = bool(filetypes or filetypes_exclude or locations
                      or locations_exclude or only_installed
                      or only_not_installed or query)
    if not any_active:
        return list(entries)

    def _matches(arc: DownloadEntry) -> bool:
        if arc.path is None:
            return False
        ext = file_extension(arc.path.name)
        if filetypes and ext not in filetypes:
            return False
        if filetypes_exclude and ext in filetypes_exclude:
            return False
        loc = _resolved(arc.src_dir)
        if locations and loc not in locations:
            return False
        if locations_exclude and loc in locations_exclude:
            return False
        inst = installed.is_archive_installed(arc.path.name)
        if only_installed == 1 and not inst:
            return False
        if only_installed == 2 and inst:
            return False
        if only_not_installed == 1 and inst:
            return False
        if only_not_installed == 2 and not inst:
            return False
        if query and query not in arc.path.name.casefold():
            return False
        return True

    result: list[DownloadEntry] = []
    i, n = 0, len(entries)
    while i < n:
        entry = entries[i]
        if entry.is_section_header:
            j = i + 1
            matched: list[DownloadEntry] = []
            while j < n and not entries[j].is_section_header:
                if _matches(entries[j]):
                    matched.append(entries[j])
                j += 1
            if matched:
                result.append(entry)
                result.extend(matched)
            i = j
        else:
            if _matches(entry):
                result.append(entry)
            i += 1
    return result
