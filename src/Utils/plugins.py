"""
plugins.py
Read and write a plugins.txt file.

Two formats are supported:

  star_prefix=True (MO2-style - Fallout 4, Skyrim SE, Starfield, …):
    *PluginName.esp   - enabled plugin
    PluginName.esp    - disabled plugin (no prefix)

  star_prefix=False (legacy engine - Fallout 3, Fallout NV, Oblivion, Skyrim LE):
    PluginName.esp    - enabled plugin
    (disabled plugins are omitted from the file entirely)

  loadorder.txt (sibling file) stores the full known plugin set as bare
  filenames; for legacy games it is the source of truth for "plugin exists
  but is disabled" (present in loadorder.txt, absent from plugins.txt).

Order in the file defines load order (line 0 = first loaded).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass
class PluginEntry:
    name: str
    enabled: bool


def primary_plugin_order(game) -> list[str]:
    """Return a game's case-preserving, de-duplicated fixed plugin order.

    An empty list is intentional: games without a known engine-defined order
    continue to use their saved order or LOOT result.
    """
    try:
        configured = list(getattr(game, "primary_plugin_order", []) or [])
    except Exception:
        return []
    result: list[str] = []
    seen: set[str] = set()
    for name in configured:
        name = str(name).strip()
        low = name.lower()
        if name and low not in seen:
            result.append(name)
            seen.add(low)
    return result


def enforce_primary_plugin_order(game, entries: list) -> "tuple[list, bool]":
    """Move present primary plugins to the front in the game's fixed order.

    Entry objects are reused (``PluginEntry`` and the GUI's ``PluginRow`` both
    expose ``.name``).  Non-primary entries retain their relative order.  The
    returned boolean says whether the incoming order changed.
    """
    original = list(entries)
    preferred = primary_plugin_order(game)
    if not preferred or not original:
        return original, False

    rank = {name.lower(): i for i, name in enumerate(preferred)}
    buckets: list[list] = [[] for _ in preferred]
    rest: list = []
    for entry in original:
        name = entry if isinstance(entry, str) else getattr(entry, "name", "")
        pos = rank.get(str(name).lower())
        if pos is None:
            rest.append(entry)
        else:
            buckets[pos].append(entry)
    ordered = [entry for bucket in buckets for entry in bucket] + rest
    return ordered, ordered != original


# Per-path mtime-keyed cache of the *parsed* plugins.txt / loadorder.txt content.
# A single plugin-tab refresh reads plugins.txt 2-3× (the two sync passes plus
# _refresh_plugins_tab) for a ~1300-line file; on a toggle that writes nothing,
# all three hit this cache. We store immutable tuples and rebuild fresh
# PluginEntry objects per call so callers can freely mutate the returned list
# without corrupting the cache. A write to either file bumps its mtime, so the
# next read re-parses automatically (no explicit invalidation needed).
_plugins_parse_cache: dict[tuple[str, bool], tuple[float, tuple[tuple[str, bool], ...]]] = {}
_loadorder_parse_cache: dict[str, tuple[float, tuple[str, ...]]] = {}


def invalidate_plugins_cache(path: "Path | None" = None) -> None:
    """Drop cached read_plugins/read_loadorder data for *path* (or all).

    Call this after writing plugins.txt by any path other than write_plugins()
    (e.g. a direct path.write_text in a wizard or collection installer) so the
    next read re-parses even on a filesystem with coarse mtime resolution.
    """
    if path is None:
        _plugins_parse_cache.clear()
        _loadorder_parse_cache.clear()
        return
    ps = str(path)
    _plugins_parse_cache.pop((ps, True), None)
    _plugins_parse_cache.pop((ps, False), None)
    _loadorder_parse_cache.pop(ps, None)


def _normalise_ext(name: str) -> str:
    """Return name with its file extension lowercased (e.g. Mod.ESP → Mod.esp)."""
    dot = name.rfind(".")
    if dot == -1:
        return name
    return name[:dot] + name[dot:].lower()


def _read_text_game_compat(path: Path) -> str:
    """Read a plugins/loadorder file tolerating either encoding.

    The game rewrites plugins.txt in Windows-1252 (loadorder.txt is UTF-8);
    we historically wrote UTF-8. ASCII content is byte-identical in both, so
    try UTF-8 first and fall back to cp1252 - a game-rewritten file with a
    non-ASCII plugin name must never crash the loader.
    """
    raw = path.read_bytes()
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("cp1252", errors="replace")


def _plugins_txt_encoding(text: str) -> str:
    """Encoding for a plugins.txt payload: cp1252 when the engine can read it
    (Bethesda engines parse plugins.txt as Windows-1252 - MO2/Vortex/LOOT write
    it that way), UTF-8 as a lossless fallback for names cp1252 can't hold."""
    try:
        text.encode("cp1252")
        return "cp1252"
    except UnicodeEncodeError:
        return "utf-8"


def read_plugins(path: Path, star_prefix: bool = True) -> list[PluginEntry]:
    """
    Parse plugins.txt and return entries in file order (index 0 = first loaded).
    Lines that are blank or start with '#' are skipped.

    When star_prefix is True (MO2-style):
      '*Name' = enabled; bare 'Name' = disabled.
    When star_prefix is False (legacy engine / Oblivion Remastered):
      All listed plugins are enabled. Disabled plugins are not present
      in the file - callers that need the full plugin set (to recover
      disabled state) should cross-reference loadorder.txt.
    """
    if not path.is_file():
        return []
    try:
        mtime = path.stat().st_mtime
    except OSError:
        mtime = None
    cache_key = (str(path), star_prefix)
    if mtime is not None:
        cached = _plugins_parse_cache.get(cache_key)
        if cached is not None and cached[0] == mtime:
            # Rebuild fresh objects so callers can mutate the list safely.
            return [PluginEntry(name=n, enabled=en) for n, en in cached[1]]
    parsed: list[tuple[str, bool]] = []
    for line in _read_text_game_compat(path).splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if star_prefix:
            if line.startswith("*"):
                parsed.append((_normalise_ext(line[1:]), True))
            else:
                parsed.append((_normalise_ext(line), False))
        else:
            parsed.append((_normalise_ext(line), True))
    if mtime is not None:
        _plugins_parse_cache[cache_key] = (mtime, tuple(parsed))
    return [PluginEntry(name=n, enabled=en) for n, en in parsed]


def write_plugins(path: Path, entries: list[PluginEntry], star_prefix: bool = True) -> None:
    """
    Write entries back to plugins.txt.
    Creates parent directories if needed.

    When star_prefix is True (MO2-style):
      Enabled entries are written as '*Name', disabled as bare 'Name'.
    When star_prefix is False (legacy engine):
      Only enabled entries are written (the engine has no '*' syntax and
      treats any listed plugin as active). Disabled entries survive in
      loadorder.txt, which is the source of truth for the full plugin set.

    Atomic (write-temp → rename) so a concurrent reader never observes a
    truncated/partial plugins.txt.
    """
    from Utils.atomic_write import write_atomic_text
    if star_prefix:
        lines = [(f"*{e.name}" if e.enabled else e.name) for e in entries]
    else:
        lines = [e.name for e in entries if e.enabled]
    text = "\n".join(lines) + ("\n" if lines else "")
    write_atomic_text(path, text, encoding=_plugins_txt_encoding(text))
    # Refresh the parse cache from what we just wrote so a same-mtime-resolution
    # write (e.g. exFAT's coarse timestamps) can't serve stale data on the next
    # read. We cache under both star_prefix variants of the parsed content.
    try:
        mtime = path.stat().st_mtime
        star_parsed = tuple(
            (_normalise_ext(e.name), e.enabled) for e in entries
        )
        _plugins_parse_cache[(str(path), True)] = (mtime, star_parsed)
        # Legacy (no-star) reads return only enabled plugins, all enabled.
        legacy_parsed = tuple(
            (_normalise_ext(e.name), True) for e in entries if e.enabled
        )
        _plugins_parse_cache[(str(path), False)] = (mtime, legacy_parsed)
    except OSError:
        _plugins_parse_cache.pop((str(path), True), None)
        _plugins_parse_cache.pop((str(path), False), None)


def insert_by_loadorder(entries: list[PluginEntry], entry: PluginEntry,
                        lo_pos: dict[str, int]) -> None:
    """Insert *entry* at the slot loadorder.txt remembers for it.

    Placed before the first existing entry that *lo_pos* (lowercase name →
    loadorder.txt index) orders later, so a re-added plugin returns to its
    old load-order slot. Names without a position append at the end."""
    pos = lo_pos.get(entry.name.lower())
    if pos is not None:
        for i, existing in enumerate(entries):
            epos = lo_pos.get(existing.name.lower())
            if epos is not None and epos > pos:
                entries.insert(i, entry)
                return
    entries.append(entry)


def sweep_plugins_variants(directory: Path, filename: str, log_fn=None,
                           keep: "Path | None" = None) -> int:
    """Delete every case-variant of `filename` in `directory`; return the count.

    Wine resolves paths case-insensitively but the prefix sits on a
    case-sensitive Linux filesystem, so `plugins.txt` and `Plugins.txt` can
    both exist on disk at once. The engine then reads whichever one Wine
    happens to resolve first, which may be a stale file we didn't write.
    `keep` (already-resolved path) is left in place.
    """
    _log = log_fn or (lambda _msg: None)
    wanted = filename.lower()
    removed = 0
    try:
        entries = list(directory.iterdir())
    except OSError:
        return 0
    for entry in entries:
        if entry.name.lower() != wanted:
            continue
        if keep is not None and entry == keep:
            continue
        try:
            entry.unlink()
        except OSError as exc:
            _log(f"  WARN: could not remove stale {entry}: {exc}")
            continue
        removed += 1
        _log(f"  Removed stale {entry.name}: {entry}")
    return removed


def deploy_plugins_copy(directory: Path, filename: str, content: str, log_fn=None) -> None:
    """Write `content` into `directory / filename` as a real file (not a symlink).

    GOG builds of Bethesda games can't read a *symlinked* plugins.txt, so we
    deploy a real copy. Any pre-existing case-variant (`Plugins.txt` next to
    `plugins.txt`) is swept first so the one we write is the only one the
    engine can resolve; games that write to a real (case-sensitive) game
    directory pass their own `filename` casing.
    """
    _log = log_fn or (lambda _msg: None)
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / filename
    try:
        sweep_plugins_variants(directory, filename, _log)
        if target.exists() or target.is_symlink():
            target.unlink()
        # The engine parses plugins.txt as Windows-1252 - write the copy it
        # actually reads in that encoding whenever the content allows it.
        target.write_text(content, encoding=_plugins_txt_encoding(content))
        _log(f"  Wrote {filename} → {target}")
    except OSError as exc:
        _log(f"  WARN: could not write {target}: {exc}")


def remove_plugins_copy(directory: Path, filename: str, log_fn=None) -> None:
    """Remove `directory / filename` (a deployed copy or legacy symlink).

    Sweeps every case-variant so restore can't leave a stray `Plugins.txt`
    behind for the engine to pick up on the next launch.
    """
    _log = log_fn or (lambda _msg: None)
    target = directory / filename
    if target.exists() or target.is_symlink():
        try:
            target.unlink()
            _log(f"  Removed {filename}: {target}")
        except OSError as exc:
            _log(f"  WARN: could not remove {target}: {exc}")
    sweep_plugins_variants(directory, filename, _log)


def read_loadorder(path: Path) -> list[str]:
    """Read loadorder.txt and return plugin names in order.

    loadorder.txt stores the full load order including vanilla plugins
    (which are excluded from plugins.txt).  One bare filename per line.
    """
    if not path.is_file():
        return []
    try:
        mtime = path.stat().st_mtime
    except OSError:
        mtime = None
    if mtime is not None:
        cached = _loadorder_parse_cache.get(str(path))
        if cached is not None and cached[0] == mtime:
            return list(cached[1])  # fresh list; names are immutable str
    names: list[str] = []
    for line in _read_text_game_compat(path).splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            names.append(line)
    if mtime is not None:
        _loadorder_parse_cache[str(path)] = (mtime, tuple(names))
    return list(names)


def write_loadorder(path: Path, entries: list[PluginEntry]) -> None:
    """Write the full load order (bare filenames) to loadorder.txt.
    Atomic (write-temp → rename), like write_plugins."""
    from Utils.atomic_write import write_atomic_text
    lines = [e.name for e in entries]
    write_atomic_text(path, "\n".join(lines) + ("\n" if lines else ""))
    # Keep the read cache coherent even under coarse (exFAT) mtime resolution.
    try:
        _loadorder_parse_cache[str(path)] = (path.stat().st_mtime, tuple(lines))
    except OSError:
        _loadorder_parse_cache.pop(str(path), None)


def append_plugin(path: Path, plugin_name: str, enabled: bool = True,
                  star_prefix: bool = True) -> None:
    """
    Append a plugin to the bottom of plugins.txt if not already present.
    The check is case-insensitive so 'Plugin.esp' and 'plugin.esp' are treated
    as the same plugin.
    Does nothing if the plugin already exists in the file.
    """
    entries = read_plugins(path, star_prefix=star_prefix)
    existing_lower = {e.name.lower() for e in entries}
    if plugin_name.lower() in existing_lower:
        return
    entries.append(PluginEntry(name=plugin_name, enabled=enabled))
    write_plugins(path, entries, star_prefix=star_prefix)


def prune_plugins_from_filemap(
    filemap_path: Path,
    plugins_path: Path,
    plugin_extensions: list[str],
    data_dir: Path | None = None,
    star_prefix: bool = True,
) -> int:
    """
    Remove entries from plugins.txt whose plugin file no longer appears in
    filemap.txt (i.e. the mod providing that plugin was disabled).

    Plugins that exist in data_dir (vanilla game plugins) are always kept,
    even if absent from the filemap.

    Only root-level files are considered (matching how Bethesda plugins work).
    Returns the count of removed entries.
    """
    if not plugin_extensions:
        return 0

    exts_lower = {ext.lower() for ext in plugin_extensions}

    # Collect all root-level plugin filenames present in the current filemap
    in_filemap: set[str] = set()
    if filemap_path.is_file():
        with filemap_path.open(encoding="utf-8") as f:
            for line in f:
                line = line.rstrip("\n")
                if "\t" not in line:
                    continue
                rel_path, _ = line.split("\t", 1)
                rel_path = rel_path.replace("\\", "/")
                if "/" in rel_path:
                    continue
                if Path(rel_path).suffix.lower() in exts_lower:
                    in_filemap.add(rel_path.lower())

    # Also keep plugins that exist as vanilla files in the game's Data/ dir.
    # Prefer Data_Core/ when it exists - after deployment Data/ contains
    # hard-linked mod files, so Data_Core/ is the reliable source of truth
    # for what plugins are truly vanilla.
    in_data_dir: set[str] = set()
    if data_dir and data_dir.is_dir():
        vanilla_dir = data_dir.parent / (data_dir.name + "_Core")
        scan_dir = vanilla_dir if vanilla_dir.is_dir() else data_dir
        for entry in scan_dir.iterdir():
            if entry.is_file() and entry.suffix.lower() in exts_lower:
                in_data_dir.add(entry.name.lower())

    keep = in_filemap | in_data_dir
    existing = read_plugins(plugins_path, star_prefix=star_prefix)
    kept = [e for e in existing if e.name.lower() in keep]
    removed = len(existing) - len(kept)
    if removed:
        write_plugins(plugins_path, kept, star_prefix=star_prefix)
    return removed


def sync_plugins_from_filemap(
    filemap_path: Path,
    plugins_path: Path,
    plugin_extensions: list[str],
    disabled_plugins: dict[str, list[str]] | None = None,
    star_prefix: bool = True,
) -> int:
    """
    Scan filemap.txt for files matching plugin_extensions and append any
    not already in plugins.txt.  Returns the count of newly added plugins.

    The filemap format is: <relative/path/to/file>\\t<mod_name>
    Only root-level files (no directory separator in relative path) are
    considered, because Bethesda plugins live at the root of the Data folder.

    disabled_plugins maps mod_name -> list of plugin filenames to suppress.
    """
    if not filemap_path.is_file() or not plugin_extensions:
        return 0

    exts_lower = {ext.lower() for ext in plugin_extensions}

    existing = read_plugins(plugins_path, star_prefix=star_prefix)
    existing_lower = {e.name.lower() for e in existing}

    # For legacy (non-star) games, a user-disabled plugin is absent from
    # plugins.txt but still present in loadorder.txt. Treat presence in
    # loadorder.txt as "already known" so we don't re-add it as enabled.
    known_lower = set(existing_lower)
    if not star_prefix:
        known_lower.update(n.lower() for n in read_loadorder(plugins_path.parent / "loadorder.txt"))

    new_entries: list[PluginEntry] = []

    with filemap_path.open(encoding="utf-8") as f:
        for line in f:
            line = line.rstrip("\n")
            if "\t" not in line:
                continue
            rel_path, mod_name = line.split("\t", 1)
            rel_path = rel_path.replace("\\", "/")
            if "/" in rel_path:
                # Plugin is inside a subfolder - not a root-level plugin file
                continue
            filename = rel_path
            if (Path(filename).suffix.lower() in exts_lower
                    and filename.lower() not in known_lower):
                if disabled_plugins:
                    mod_disabled = {n.lower() for n in disabled_plugins.get(mod_name, [])}
                    if filename.lower() in mod_disabled:
                        continue
                # Normalise extension to lowercase so case-sensitive filesystems
                # (Linux) can locate the file on disk (e.g. .ESP → .esp).
                stem = Path(filename).stem
                ext  = Path(filename).suffix.lower()
                normalised = stem + ext
                new_entries.append(PluginEntry(name=normalised, enabled=True))
                known_lower.add(normalised.lower())

    if new_entries:
        write_plugins(plugins_path, existing + new_entries, star_prefix=star_prefix)

    return len(new_entries)
