"""
loot_sorter.py
Sort a plugins.txt load order using libloot's masterlist-based sorting.

Usage:
    from LOOT.loot_sorter import sort_plugins, is_available

The sort_plugins() function takes the current plugin entries and game info,
feeds them through libloot's sorting algorithm, and returns the reordered
list with enabled/disabled state preserved.

Game support is driven by the game handler's properties:
  - loot_sort_enabled: bool       — whether LOOT sorting applies to this game
  - loot_game_type: str           — libloot GameType attribute name (e.g. 'SkyrimSE')
  - loot_masterlist_repo: str     — GitHub repo slug under github.com/loot/
                                    (e.g. 'skyrimse', 'fallout4'); used to build
                                    the masterlist URL keyed to the bundled
                                    libloot version.
  - loot_masterlist_url: str      — (legacy) full URL; used as a fallback if
                                    loot_masterlist_repo is not set.
  - game_id: str                  — used to derive the masterlist filename
                                    (masterlist_<game_id>.yaml in
                                    ~/.config/AmethystModManager/LOOT/data/)
"""

from __future__ import annotations

import json
import os
import shutil
import time
import urllib.request
import urllib.error
from pathlib import Path
from dataclasses import dataclass, field

from Utils.atomic_write import write_atomic_text

try:
    import loot
    _AVAILABLE = True
except ImportError as _exc:
    loot = None
    _AVAILABLE = False
    # A missing or ABI-mismatched extension disables LOOT sorting for the
    # whole session — record why instead of failing silently (e.g. a flatpak
    # runtime whose Python version has no matching loot.cpython-3XX .so).
    try:
        import sysconfig
        _tag = sysconfig.get_config_var("EXT_SUFFIX") or ""
        # The extension lives at the src/ root (top-level `import loot`);
        # also glob this dir to flag strays from the old LOOT/-local layout.
        _here = Path(__file__).parent
        _present = sorted(
            {p.name for d in (_here.parent, _here) for p in d.glob("loot.*.so")}
        )
        _IMPORT_DETAIL = (
            "LOOT: native extension failed to import — sorting disabled. "
            f"Interpreter needs 'loot{_tag}', found {_present or 'none'}. ({_exc})"
        )
    except Exception:
        _IMPORT_DETAIL = f"LOOT: native extension failed to import: {_exc}"
    # Best-effort: app_log drops messages until the GUI registers its sink,
    # so unavailable_reason() re-surfaces this when the user tries to sort.
    try:
        from Utils.app_log import app_log
        app_log(_IMPORT_DETAIL)
    except Exception:
        pass
else:
    _IMPORT_DETAIL = None

# Bundled masterlists shipped with the application (read-only in AppImage)
_BUNDLED_DATA_DIR = Path(__file__).parent / "data"

# User-writable masterlist directory — resolved lazily to avoid import-time side effects
_DATA_DIR: Path | None = None

# Re-download masterlists at most once per 24 hours (when libloot version
# is unchanged — a libloot version bump always forces a re-fetch).
_MASTERLIST_TTL_SECS = 86400

# Walk-down floor: don't probe branches older than this. v0.21 is the oldest
# masterlist branch any of our games currently use.
_BRANCH_FLOOR = (0, 21)

# Per-minor cap when walking back through a previous major (defensive only —
# real masterlist repos haven't crossed a 1.0 boundary).
_PREV_MAJOR_MINOR_CAP = 50

_BRANCH_CACHE_FILE = "masterlist_branches.json"


def _get_data_dir() -> Path:
    global _DATA_DIR
    if _DATA_DIR is None:
        from Utils.config_paths import get_loot_data_dir
        _DATA_DIR = get_loot_data_dir()
    return _DATA_DIR


PRELUDE_FILE = "masterlist_prelude.yaml"
# Prelude lives in github.com/loot/prelude — same per-libloot-version branch scheme.
_PRELUDE_REPO = "prelude"
_PRELUDE_FILENAME_IN_REPO = "prelude.yaml"


def _libloot_version_str() -> str:
    """e.g. '0.29.4'. Used as a cache invalidation key."""
    if not _AVAILABLE:
        return "unknown"
    try:
        return str(loot.libloot_version())
    except Exception:
        return f"{loot.LIBLOOT_VERSION_MAJOR}.{loot.LIBLOOT_VERSION_MINOR}.{loot.LIBLOOT_VERSION_PATCH}"


def _libloot_branch_target() -> tuple[int, int]:
    """Major/minor pair we'd like the masterlist branch to match.
    e.g. libloot 0.29.4 -> (0, 29) -> we look for branch 'v0.29'.
    """
    if not _AVAILABLE:
        return _BRANCH_FLOOR
    return (loot.LIBLOOT_VERSION_MAJOR, loot.LIBLOOT_VERSION_MINOR)


def _branch_label(major: int, minor: int) -> str:
    return f"v{major}.{minor}"


def _build_masterlist_url(repo: str, branch: str, filename: str = "masterlist.yaml") -> str:
    return f"https://raw.githubusercontent.com/loot/{repo}/{branch}/{filename}"


def _read_branch_cache() -> dict:
    path = _get_data_dir() / _BRANCH_CACHE_FILE
    if not path.is_file():
        return {}
    try:
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    # Invalidate cache if libloot version differs.
    if data.get("libloot_version") != _libloot_version_str():
        return {}
    return data


def _write_branch_cache(cache: dict) -> None:
    path = _get_data_dir() / _BRANCH_CACHE_FILE
    cache["libloot_version"] = _libloot_version_str()
    try:
        write_atomic_text(path, json.dumps(cache, indent=2))
    except Exception:
        pass


def _head_ok(url: str, timeout: float = 5.0) -> bool:
    """True if a HEAD request to url returns 2xx."""
    try:
        req = urllib.request.Request(url, method="HEAD")
        from Utils.ca_bundle import get_ssl_context
        with urllib.request.urlopen(req, timeout=timeout, context=get_ssl_context()) as resp:
            return 200 <= getattr(resp, "status", 200) < 300
    except urllib.error.HTTPError as e:
        return 200 <= e.code < 300
    except Exception:
        return False


def _walk_down_branches(start: tuple[int, int]) -> list[tuple[int, int]]:
    """Yield (major, minor) pairs from start down to _BRANCH_FLOOR (inclusive)."""
    out: list[tuple[int, int]] = []
    major, minor = start
    floor_major, floor_minor = _BRANCH_FLOOR
    while True:
        if (major, minor) < (floor_major, floor_minor):
            break
        out.append((major, minor))
        if minor > 0:
            minor -= 1
        else:
            if major <= 0:
                break
            major -= 1
            minor = _PREV_MAJOR_MINOR_CAP
    return out


def _resolve_branch(
    repo: str,
    filename: str = "masterlist.yaml",
    log_fn=None,
) -> str | None:
    """Find the highest 'vMAJOR.MINOR' branch <= bundled libloot that contains
    the given file in `repo`. Cached per (libloot_version, repo, filename).
    Returns the branch label (e.g. 'v0.29') or None if nothing matched.
    """
    _log = log_fn or (lambda _: None)
    cache_key = f"{repo}::{filename}"
    cache = _read_branch_cache()
    branches = cache.get("branches") or {}
    if cache_key in branches:
        return branches[cache_key]

    target = _libloot_branch_target()
    for major, minor in _walk_down_branches(target):
        branch = _branch_label(major, minor)
        url = _build_masterlist_url(repo, branch, filename)
        if _head_ok(url):
            branches[cache_key] = branch
            cache["branches"] = branches
            _write_branch_cache(cache)
            if (major, minor) != target:
                _log(
                    f"loot/{repo}: no {_branch_label(*target)} branch — "
                    f"falling back to {branch}."
                )
            return branch

    # Nothing found. Cache the negative result so we don't probe every launch.
    branches[cache_key] = None
    cache["branches"] = branches
    _write_branch_cache(cache)
    _log(f"loot/{repo}: no compatible masterlist branch found.")
    return None


def masterlist_url_for_repo(repo: str, log_fn=None) -> str:
    """Build the masterlist URL for `loot/<repo>` matching the bundled libloot
    version, walking down to the most recent available branch if the exact
    version branch doesn't exist. Returns an empty string if none found.
    """
    if not repo:
        return ""
    branch = _resolve_branch(repo, "masterlist.yaml", log_fn=log_fn)
    if not branch:
        return ""
    return _build_masterlist_url(repo, branch, "masterlist.yaml")


def prelude_url(log_fn=None) -> str:
    """URL for the LOOT prelude.yaml matching the bundled libloot."""
    branch = _resolve_branch(_PRELUDE_REPO, _PRELUDE_FILENAME_IN_REPO, log_fn=log_fn)
    if not branch:
        return ""
    return _build_masterlist_url(_PRELUDE_REPO, branch, _PRELUDE_FILENAME_IN_REPO)


def _version_sidecar(dest: Path) -> Path:
    return dest.with_suffix(dest.suffix + ".libloot_version")


def _read_sidecar_version(dest: Path) -> str:
    sidecar = _version_sidecar(dest)
    if not sidecar.is_file():
        return ""
    try:
        return sidecar.read_text(encoding="utf-8").strip()
    except Exception:
        return ""


def _write_sidecar_version(dest: Path) -> None:
    sidecar = _version_sidecar(dest)
    try:
        sidecar.write_text(_libloot_version_str(), encoding="utf-8")
    except Exception:
        pass


def _ensure_masterlist(
    filename: str,
    download_url: str = "",
    log_fn=None,
) -> None:
    """Ensure a masterlist exists in the config dir, fetching if stale.

    Resolution order:
      1. If libloot version changed since the cached file was written,
         force a re-download (the branch likely moved).
      2. If a cached file exists and is younger than _MASTERLIST_TTL_SECS, use it.
      3. Download from the provided URL.
         Falls back to the existing cached file if the download fails.
      4. If no URL or download fails and no cached file: copy from bundled data dir.
    """
    _log = log_fn or (lambda _: None)
    data_dir = _get_data_dir()
    dest = data_dir / filename

    current_version = _libloot_version_str()
    version_changed = (
        dest.is_file()
        and _read_sidecar_version(dest) != current_version
    )

    # Skip download if the cached file is fresh enough AND libloot version
    # hasn't changed since we last fetched it.
    if dest.is_file() and download_url and not version_changed:
        age = time.time() - dest.stat().st_mtime
        if age < _MASTERLIST_TTL_SECS:
            return

    # Try to download if a URL is provided
    if download_url:
        if version_changed:
            _log(f"libloot version changed — refreshing {filename}...")
        else:
            _log(f"Fetching latest {filename}...")
        try:
            # download_file writes to a .part temp then atomically renames.
            from Utils.ca_bundle import download_file
            download_file(download_url, dest)
            _write_sidecar_version(dest)
            _log(f"Updated {filename}.")
            return
        except Exception as exc:
            _log(f"Could not fetch {filename}: {exc} — using cached copy.")

    # Fall through: use cached file if it exists
    if dest.is_file():
        return

    # Last resort: copy from bundled data dir
    src = _BUNDLED_DATA_DIR / filename
    if src.is_file():
        shutil.copy2(src, dest)
        _write_sidecar_version(dest)
        _log(f"Copied bundled {filename} to config dir.")


def _masterlist_filename(game_type_attr: str) -> str:
    """Derive the masterlist filename from the libloot game type attribute name.
    e.g. 'SkyrimSE' → 'masterlist_skyrimse.yaml'
    """
    return f"masterlist_{game_type_attr.lower()}.yaml"


def is_available() -> bool:
    """Return True if libloot is importable and ready to use."""
    return _AVAILABLE


def unavailable_reason() -> str | None:
    """Why the libloot extension failed to import, or None when available."""
    return _IMPORT_DETAIL


@dataclass
class SortResult:
    """Result of a LOOT sort operation."""
    sorted_names: list[str]
    moved_count: int
    warnings: list[str]
    # plugin name (original case) -> {
    #   "messages":        list[{"type","text"}],
    #   "requirements":    list[{"name","display_name","detail"}],
    #   "incompatibilities": list[{"name","display_name","detail"}],
    #   "locations":       list[{"name","url"}],
    #   "dirty":           list[{"crc","itm","udr","nav","utility","detail"}],
    #   "clean":           list[{"crc","itm","udr","nav","utility","detail"}],
    #   "tags":            {"current":[...], "add":[...], "remove":[...]},
    # }
    # dirty/clean are CRC-filtered by libloot when conditions are evaluated.
    # Populated from evaluated masterlist/userlist metadata (conditions applied).
    plugin_info: dict[str, dict] = field(default_factory=dict)
    # General (masterlist-wide) messages: list[{"type","text"}].
    general_messages: list[dict] = field(default_factory=list)


_MESSAGE_TYPE_NAMES = {
    0: "say",
    1: "warn",
    2: "error",
}


def _msg_type_name(mt) -> str:
    """Map a libloot MessageType enum to a lowercase string."""
    try:
        return _MESSAGE_TYPE_NAMES.get(int(mt), "say")
    except (TypeError, ValueError):
        name = getattr(mt, "name", str(mt)).lower()
        if name in ("say", "warn", "error"):
            return name
        return "say"


def _extract_message_text(message, language: str = "en") -> str:
    """Pick the best-matching localised string out of a libloot Message."""
    contents = list(message.content) if message.content else []
    if not contents:
        return ""
    picked = None
    try:
        picked = loot.select_message_content(contents, language)
    except Exception:
        picked = None
    if picked is None:
        for c in contents:
            if getattr(c, "language", "") in (language, "en", ""):
                picked = c
                break
        if picked is None:
            picked = contents[0]
    return getattr(picked, "text", "") or ""


def _render_messages(msgs, language: str = "en") -> list[dict]:
    rendered: list[dict] = []
    for m in msgs or []:
        text = _extract_message_text(m, language)
        if not text:
            continue
        rendered.append({
            "type": _msg_type_name(m.message_type),
            "text": text,
        })
    return rendered


def _render_file_list(files) -> list[dict]:
    """Render a libloot File list (requirements / incompatibilities)."""
    out: list[dict] = []
    for f in files or []:
        # File.name is a Filename type; str() gives the plugin/file string.
        name = str(getattr(f, "name", "") or "")
        display = getattr(f, "display_name", "") or ""
        detail_msgs = getattr(f, "detail", None)
        # detail is a list[MessageContent] — pick best-match language.
        detail_text = ""
        if detail_msgs:
            try:
                picked = loot.select_message_content(list(detail_msgs), "en")
            except Exception:
                picked = None
            if picked is None and detail_msgs:
                picked = list(detail_msgs)[0]
            if picked is not None:
                detail_text = getattr(picked, "text", "") or ""
        out.append({
            "name": name,
            "display_name": display or name,
            "detail": detail_text,
        })
    return out


def _render_locations(locs) -> list[dict]:
    out: list[dict] = []
    for l in locs or []:
        name = getattr(l, "name", "") or ""
        url = getattr(l, "url", "") or ""
        if not url:
            continue
        out.append({"name": name or url, "url": url})
    return out


def _render_cleaning_data(entries, language: str = "en") -> list[dict]:
    """Render a libloot PluginCleaningData list (dirty / clean info).

    When metadata is fetched with evaluate_conditions=True, libloot already
    filters these against the installed plugin's CRC, so every entry here
    applies to the plugin's actual on-disk version.
    """
    out: list[dict] = []
    for d in entries or []:
        # detail is a list[MessageContent] — pick best-match language.
        detail_text = ""
        detail_msgs = getattr(d, "detail", None)
        if detail_msgs:
            try:
                picked = loot.select_message_content(list(detail_msgs), language)
            except Exception:
                picked = None
            if picked is None:
                picked = list(detail_msgs)[0]
            if picked is not None:
                detail_text = getattr(picked, "text", "") or ""
        out.append({
            "crc": getattr(d, "crc", 0),
            "itm": getattr(d, "itm_count", 0),
            "udr": getattr(d, "deleted_reference_count", 0),
            "nav": getattr(d, "deleted_navmesh_count", 0),
            "utility": getattr(d, "cleaning_utility", "") or "",
            "detail": detail_text,
        })
    return out


def _render_bash_tags(current, suggested) -> dict:
    """Merge a plugin's current (header) Bash Tags with masterlist suggestions.

    current   — iterable of tag-name strings from plugin.bash_tags()
    suggested — iterable of libloot Tag objects from evaluated metadata
                (each has .name and .is_addition).

    Returns {"current": [...], "add": [...], "remove": [...]} with empty lists
    omitted by the caller. Suggested additions already present in current are
    dropped so the UI only highlights tags the user would still need to add.
    """
    cur = sorted({str(t) for t in (current or []) if str(t)})
    cur_set = {c.lower() for c in cur}
    add: list[str] = []
    remove: list[str] = []
    for t in suggested or []:
        name = getattr(t, "name", "") or ""
        if not name:
            continue
        if getattr(t, "is_addition", True):
            if name.lower() not in cur_set:
                add.append(name)
        else:
            remove.append(name)
    out: dict = {}
    if cur:
        out["current"] = cur
    if add:
        out["add"] = sorted(set(add))
    if remove:
        out["remove"] = sorted(set(remove))
    return out


def _collect_plugin_info(
    db,
    plugin_names: list[str],
    game=None,
    language: str = "en",
) -> dict[str, dict]:
    """Collect evaluated masterlist/userlist metadata for each plugin.

    Returns a mapping of plugin name -> info dict (see SortResult.plugin_info).
    Only plugins that have at least one non-empty field are included.

    When `game` is provided (the libloot Game with plugin headers loaded),
    each plugin's current header Bash Tags are merged with the masterlist's
    suggested add/remove tags.
    """
    out: dict[str, dict] = {}
    for name in plugin_names:
        try:
            meta = db.plugin_metadata(name, True, True)
        except Exception:
            continue
        if meta is None:
            continue

        messages = _render_messages(meta.messages, language)
        requirements = _render_file_list(meta.requirements)
        incompatibilities = _render_file_list(meta.incompatibilities)
        locations = _render_locations(meta.locations)
        # dirty_info / clean_info are CRC-filtered by libloot when conditions
        # are evaluated, so they reflect the plugin's installed version.
        dirty = _render_cleaning_data(meta.dirty_info, language)
        clean = _render_cleaning_data(meta.clean_info, language)

        # Bash Tags: current (header) tags from the plugin object, merged with
        # the masterlist's suggested additions/removals.
        current_tags = []
        if game is not None:
            try:
                pl = game.plugin(name)
                if pl is not None:
                    current_tags = list(pl.bash_tags())
            except Exception:
                current_tags = []
        tags = _render_bash_tags(current_tags, meta.tags)

        if not (messages or requirements or incompatibilities or locations
                or dirty or clean or tags):
            continue

        info: dict = {}
        if messages:
            info["messages"] = messages
        if requirements:
            info["requirements"] = requirements
        if incompatibilities:
            info["incompatibilities"] = incompatibilities
        if locations:
            info["locations"] = locations
        if dirty:
            info["dirty"] = dirty
        if clean:
            info["clean"] = clean
        if tags:
            info["tags"] = tags
        out[name] = info
    return out


def _collect_general_messages(db, language: str = "en") -> list[dict]:
    try:
        gen = db.general_messages(True)
    except Exception:
        return []
    out: list[dict] = []
    for m in gen or []:
        text = _extract_message_text(m, language)
        if not text:
            continue
        out.append({"type": _msg_type_name(m.message_type), "text": text})
    return out


def write_loot_info(
    profile_dir: Path,
    plugin_info: dict[str, dict],
    general_messages: list[dict],
    game_id: str = "",
) -> None:
    """Persist evaluated LOOT metadata to <profile_dir>/loot.json (atomic).

    Schema v3: plugin_info values are dicts {messages, requirements,
    incompatibilities, locations, dirty, clean, tags}. Fields are omitted when
    empty. dirty/clean are CRC-filtered PluginCleaningData lists (see
    _render_cleaning_data); tags merges header + masterlist Bash Tags (see
    _render_bash_tags).
    """
    profile_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": 3,
        "generated_at": int(time.time()),
        "game_id": game_id,
        "general_messages": general_messages,
        "plugins": plugin_info,
    }
    write_atomic_text(profile_dir / "loot.json",
                      json.dumps(payload, indent=2, ensure_ascii=False))


def read_loot_info(profile_dir: Path) -> dict:
    """Read loot.json from a profile dir. Returns empty dict on any failure."""
    path = profile_dir / "loot.json"
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


_PLUGIN_MAGIC = (b"TES3", b"TES4")


def _is_valid_plugin_file(path: Path) -> bool:
    """True if the file looks like a parseable Bethesda plugin (size + magic)."""
    try:
        if path.stat().st_size < 24:
            return False
        with path.open("rb") as f:
            return f.read(4) in _PLUGIN_MAGIC
    except OSError:
        return False


def _read_filemap_winners(
    staging_root: Path | None,
    needed_lower: set[str],
) -> dict[str, Path]:
    """Resolve plugin names to the staging file of their *winning* enabled mod.

    `filemap.txt` (Profiles/<game>/filemap.txt) maps each deployed relative
    path to the mod folder that wins the conflict — it already encodes both the
    enabled/disabled state and mod priority. We use it to pin each plugin to the
    copy that would actually be deployed, rather than letting an arbitrary
    staging tree walk return a *disabled* mod's copy (e.g. a stale ESLifier
    "cleaned" plugin left in staging after the mod is turned off).

    Root-level plugins appear in filemap.txt as a bare basename key, which is
    exactly what we match on. Returns a map of lowercase basename → resolved
    staging path, only for names whose winning mod folder actually contains the
    file. Names without a usable filemap entry are omitted so the caller can
    fall back to the tree walk (covers a stale/missing filemap).
    """
    if not staging_root or not needed_lower:
        return {}
    filemap_path = staging_root.parent / "filemap.txt"
    if not filemap_path.is_file():
        return {}

    resolved: dict[str, Path] = {}
    try:
        with filemap_path.open("r", encoding="utf-8", errors="replace") as f:
            for line in f:
                tab = line.find("\t")
                if tab < 0:
                    continue
                rel = line[:tab]
                # Only root-level plugin entries (bare basename, no subfolder)
                # correspond to a loadable plugin name.
                if "/" in rel or "\\" in rel:
                    continue
                rel_lower = rel.lower()
                if rel_lower not in needed_lower or rel_lower in resolved:
                    continue
                mod = line[tab + 1:].rstrip("\r\n")
                if not mod:
                    continue
                candidate = staging_root / mod / rel
                if candidate.is_file():
                    resolved[rel_lower] = candidate
    except OSError:
        return resolved
    return resolved


def _scan_tree_for_plugins(
    root: Path,
    needed_lower: set[str],
    plugin_exts: set[str] | None = None,
) -> dict[str, Path]:
    """Walk a directory tree (depth-first) looking for files whose lowercase
    basename is in `needed_lower`.

    Returns a map of lowercase basename → first matching path, stopping the walk
    as soon as every needed name has been located. Uses os.scandir so directory
    entries are filtered without a stat per file — the bulk of mod data (meshes,
    textures, …) never gets stat'd, only candidate plugin files do.

    When `plugin_exts` is given (e.g. {".esp", ".esm", ".esl"}), matches are
    further restricted to those extensions; pass None to match by basename only.
    """
    if not needed_lower:
        return {}
    remaining = set(needed_lower)
    found: dict[str, Path] = {}
    stack: list[str] = [str(root)]
    while stack and remaining:
        try:
            entries = list(os.scandir(stack.pop()))
        except OSError:
            continue
        for entry in entries:
            try:
                if entry.is_dir(follow_symlinks=False):
                    stack.append(entry.path)
                    continue
            except OSError:
                continue
            name_lower = entry.name.lower()
            if name_lower not in remaining:
                continue
            if plugin_exts is not None:
                ext = name_lower.rsplit(".", 1)
                if len(ext) != 2 or ("." + ext[1]) not in plugin_exts:
                    continue
            found[name_lower] = Path(entry.path)
            remaining.discard(name_lower)
            if not remaining:
                break
    return found


def _find_plugin_paths(
    plugin_names: list[str],
    game_data_dir: Path,
    staging_root: Path | None,
) -> tuple[list[str], list[str]]:
    """
    Locate plugin files on disk, searching the game's Data directory first,
    then falling back to the mod staging folders, then the overwrite folder.

    Empty or malformed plugin files are skipped — libloot can hang or crash
    when handed a 0-byte or non-TES file.

    Returns:
        (found_paths, missing_names)
    """
    found: dict[str, str] = {}  # plugin name → absolute path
    # Track basenames already added to prevent duplicate filenames (libloot
    # requires every path to have a unique filename, case-insensitively).
    found_basenames: set[str] = set()
    names_lower = {n.lower(): n for n in plugin_names}

    # 1. Check the game's Data directory. Resolution must be case-insensitive:
    #    plugins.txt names come from manifests (.ccc) / mod metadata, while some
    #    installs ship the files lowercased (e.g. Skyrim AE Creation Club
    #    content). A case-sensitive miss here marks the plugin "missing", which
    #    knocks it out of the sort and used to dump CC content below the mods.
    if game_data_dir.is_dir():
        data_index: dict[str, str] = {}
        try:
            for entry in os.scandir(game_data_dir):
                if entry.is_file():
                    data_index.setdefault(entry.name.lower(), entry.name)
        except OSError:
            pass
        for name in plugin_names:
            low = name.lower()
            real = data_index.get(low)
            if real is None or low in found_basenames:
                continue
            full = game_data_dir / real
            if _is_valid_plugin_file(full):
                found[name] = str(full)
                found_basenames.add(low)

    # 2. For anything still missing, search the mod staging folders.
    if staging_root and staging_root.is_dir():
        missing_lower = {n.lower() for n in plugin_names if n not in found}
        if missing_lower:
            # 2a. Prefer the filemap winner so we read the *enabled* mod's copy
            #     and not a disabled mod's leftover (e.g. an ESLifier "cleaned"
            #     plugin still in staging after the mod is turned off). This
            #     resolution drives the CRC libloot uses for dirty/clean flags.
            for name_lower, path in _read_filemap_winners(
                    staging_root, missing_lower).items():
                orig = names_lower.get(name_lower)
                if (orig and orig not in found
                        and name_lower not in found_basenames
                        and _is_valid_plugin_file(path)):
                    found[orig] = str(path)
                    found_basenames.add(name_lower)

            # 2b. For anything the filemap couldn't resolve (stale/missing
            #     filemap), fall back to a recursive scan. The scan walks via
            #     os.scandir and stops as soon as every missing plugin is
            #     located, skipping the bulk of mod data.
            missing_lower = {n.lower() for n in plugin_names if n not in found}
            for name_lower, path in _scan_tree_for_plugins(
                    staging_root, missing_lower).items():
                orig = names_lower.get(name_lower)
                if (orig and orig not in found
                        and name_lower not in found_basenames
                        and _is_valid_plugin_file(path)):
                    found[orig] = str(path)
                    found_basenames.add(name_lower)

    # 3. For anything still missing, check the overwrite folder
    #    (staging_root's sibling: Profiles/<game>/overwrite/)
    if staging_root:
        overwrite_dir = staging_root.parent / "overwrite"
        if overwrite_dir.is_dir():
            missing_lower = {n.lower() for n in plugin_names if n not in found}
            if missing_lower:
                for name_lower, path in _scan_tree_for_plugins(
                        overwrite_dir, missing_lower).items():
                    orig = names_lower.get(name_lower)
                    if (orig and orig not in found
                            and name_lower not in found_basenames
                            and _is_valid_plugin_file(path)):
                        found[orig] = str(path)
                        found_basenames.add(name_lower)

    found_paths = [found[n] for n in plugin_names if n in found]
    missing_names = [n for n in plugin_names if n not in found]
    return found_paths, missing_names


def sort_plugins(
    plugin_names: list[str],
    enabled_set: set[str],
    game_name: str,
    game_path: Path,
    staging_root: Path | None = None,
    log_fn=None,
    game_type_attr: str = "",
    game_id: str = "",
    masterlist_url: str = "",
    masterlist_repo: str = "",
    game_data_dir: Path | None = None,
    userlist_path: Path | None = None,
) -> SortResult:
    """
    Sort plugins using libloot's masterlist rules.

    Args:
        plugin_names: Current plugin names in load order.
        enabled_set: Set of plugin names (case-sensitive) that are enabled.
        game_name: Display name of the game (for log messages).
        game_path: Root install directory of the game.
        staging_root: Path to the mod staging directory (Profiles/<game>/mods/).
        log_fn: Optional callback for status messages.
        game_type_attr: libloot GameType attribute name (e.g. 'SkyrimSE').
                        Obtained from game.loot_game_type.
        game_id: Game ID used to locate the masterlist file
                 (~/.config/AmethystModManager/LOOT/data/masterlist_<game_id>.yaml).
                 Obtained from game.game_id.

    Returns:
        SortResult with the new order, count of moved plugins, and any warnings.

    Raises:
        RuntimeError: If libloot is not available or no masterlist is found.
    """
    _log = log_fn or (lambda _: None)

    if not _AVAILABLE:
        raise RuntimeError(
            "libloot is not available. "
            "Rebuild it with: ./LOOT/rebuild_libloot.sh"
        )

    # Deduplicate plugin_names case-insensitively: keep first occurrence of each
    # unique lowercase name. This prevents duplicate paths being generated when
    # the same plugin appears more than once with different capitalisation.
    seen_lower: set[str] = set()
    deduped: list[str] = []
    for n in plugin_names:
        nl = n.lower()
        if nl not in seen_lower:
            seen_lower.add(nl)
            deduped.append(n)
    plugin_names = deduped

    if not game_type_attr:
        raise RuntimeError(
            f"No LOOT game type configured for '{game_name}'. "
            "Set loot_game_type in the game's Python handler."
        )

    game_type = getattr(loot.GameType, game_type_attr, None)
    if game_type is None:
        raise RuntimeError(
            f"Unknown libloot GameType '{game_type_attr}' for '{game_name}'."
        )

    ml_filename = _masterlist_filename(game_type_attr)

    # Prefer the per-libloot-version branch resolver when a repo slug is set;
    # fall back to a legacy hardcoded URL if the game handler still passes one.
    effective_masterlist_url = (
        masterlist_url_for_repo(masterlist_repo, log_fn=_log)
        if masterlist_repo
        else masterlist_url
    )

    _ensure_masterlist(ml_filename, download_url=effective_masterlist_url, log_fn=_log)
    _ensure_masterlist(PRELUDE_FILE, download_url=prelude_url(log_fn=_log), log_fn=_log)

    loot_data_dir = _get_data_dir()
    masterlist_path = loot_data_dir / ml_filename
    prelude_path = loot_data_dir / PRELUDE_FILE

    if not masterlist_path.is_file():
        url_hint = (f"\nDownload from: {effective_masterlist_url}"
                    if effective_masterlist_url
                    else "\nDownload it from the LOOT GitHub repository.")
        raise RuntimeError(
            f"Masterlist not found: {masterlist_path}{url_hint}"
        )

    warnings: list[str] = []

    # libloot's load_current_load_order_state() reads plugin headers directly
    # from the game's Data directory to determine flags such as the ESM/master
    # flag.  Masterlist conditions like is_master() rely on this — if a plugin
    # is only in the staging folder (i.e. the profile is not deployed), libloot
    # cannot see its header and treats it as a non-master, which can trigger
    # spurious cyclic-dependency errors when conditional load-after rules fire
    # incorrectly.  To fix this, temporarily symlink any staging plugins that
    # are absent from Data/ into Data/ before creating the Game instance, then
    # remove the symlinks in the finally block.
    effective_data_dir = game_data_dir if game_data_dir is not None else game_path / "Data"
    _plugin_exts = {".esp", ".esm", ".esl"}
    _temp_data_symlinks: list[Path] = []

    if staging_root and staging_root.is_dir() and effective_data_dir.is_dir():
        # We only need to symlink plugins that aren't already present in Data/.
        # For a deployed profile every plugin is already there, so this set is
        # empty and we skip the (potentially huge) staging tree walk entirely.
        # Presence is checked case-insensitively — some installs ship
        # differently-cased plugin files (e.g. lowercased CC content), and a
        # case-sensitive miss would symlink a second, differently-cased copy.
        data_present_lower: set[str] = set()
        try:
            data_present_lower = {e.name.lower()
                                  for e in os.scandir(effective_data_dir)}
        except OSError:
            pass
        needed: set[str] = set()
        for name in plugin_names:
            if name.lower() in data_present_lower:
                continue
            needed.add(name.lower())

        if needed:
            # Build a map of lowercase plugin name → staging file path. Prefer
            # the filemap winner (the enabled mod's copy) so the header — and
            # the CRC used for dirty/clean flags — comes from the file that
            # would actually deploy, not a disabled mod's leftover copy. Fall
            # back to a recursive scan for anything the filemap can't resolve.
            staging_plugin_map = _read_filemap_winners(staging_root, needed)
            still_needed = needed - set(staging_plugin_map)
            if still_needed:
                staging_plugin_map.update(_scan_tree_for_plugins(
                    staging_root, still_needed, _plugin_exts,
                ))
            for src in staging_plugin_map.values():
                dest = effective_data_dir / src.name
                if not dest.exists() and not dest.is_symlink():
                    try:
                        dest.symlink_to(src)
                        _temp_data_symlinks.append(dest)
                    except OSError:
                        pass

    try:
        # Create libloot Game instance
        local_data = str(loot_data_dir)
        _log("Initializing LOOT...")
        game = loot.Game(game_type, str(game_path), local_data)
        game.load_current_load_order_state()

        # Load masterlist
        db = game.database()
        if prelude_path.is_file():
            db.load_masterlist_with_prelude(str(masterlist_path), str(prelude_path))
            _log("Loaded masterlist with prelude.")
        else:
            db.load_masterlist(str(masterlist_path))
            _log("Loaded masterlist (no prelude found).")
            warnings.append("Prelude file not found — sorting may be less accurate.")

        # Load userlist if provided and non-empty
        if userlist_path is not None and userlist_path.is_file():
            try:
                content = userlist_path.read_text(encoding="utf-8")
                if content.strip():
                    db.load_userlist(str(userlist_path))
                    _log(f"Loaded userlist: {userlist_path.name}")
            except (ValueError, OSError) as e:
                warnings.append(f"Userlist skipped: {e}")

        # Find plugin files on disk — check game Data dir AND staging mods
        plugin_paths, missing = _find_plugin_paths(
            plugin_names, effective_data_dir, staging_root,
        )

        if missing:
            warnings.append(
                f"{len(missing)} plugin(s) not found on disk: "
                f"{', '.join(missing[:5])}"
                + (f" ... and {len(missing)-5} more" if len(missing) > 5 else "")
            )

        if plugin_paths:
            _log(f"Loading {len(plugin_paths)} plugin headers...")
            game.load_plugin_headers(plugin_paths)

        # Only sort plugins that exist on disk — libloot can't sort unknown plugins
        missing_set = set(missing)
        sortable = [n for n in plugin_names if n not in missing_set]
        unsortable = [n for n in plugin_names if n in missing_set]

        _log(f"Sorting {len(sortable)} plugins...")

        try:
            sorted_names = game.sort_plugins(sortable)
        except loot.CyclicInteractionError as e:
            raise RuntimeError(f"LOOT found a cyclic dependency: {e}") from e

        # Count how many sortable plugins actually moved position
        moved = sum(
            1 for i, name in enumerate(sorted_names[:len(sortable)])
            if i >= len(sortable) or sortable[i] != name
        )

        # Re-insert unsortable plugins (missing from disk) at their original
        # positions rather than appending them at the end: appending would move
        # them below every mod plugin, breaking the load order for entries that
        # belong near the top (e.g. Creation Club content the resolver couldn't
        # find). The sorted plugins fill the remaining slots in LOOT's order.
        if unsortable:
            if len(sorted_names) == len(sortable):
                it = iter(sorted_names)
                sorted_names = [n if n in missing_set else next(it)
                                for n in plugin_names]
            else:
                # libloot returned a different plugin set than it was given
                # (shouldn't happen) — positional merge is impossible, fall
                # back to appending so no plugin is dropped.
                sorted_names = sorted_names + unsortable

        _log(f"Sort complete. {moved} plugin(s) changed position.")

        # Collect evaluated metadata for every plugin. Conditions are evaluated
        # against the plugin headers we just loaded, so this reflects the live
        # state of the current profile. This includes dirty/clean info: libloot
        # CRC-matches those entries during condition evaluation, so they're
        # filtered to each plugin's installed version with no full-body load.
        try:
            plugin_info = _collect_plugin_info(db, sortable, game=game)
            general_msgs = _collect_general_messages(db)
            if plugin_info:
                _log(f"Collected LOOT metadata for {len(plugin_info)} plugin(s).")
        except Exception as e:
            plugin_info = {}
            general_msgs = []
            warnings.append(f"Could not collect LOOT metadata: {e}")

        return SortResult(
            sorted_names=sorted_names,
            moved_count=moved,
            warnings=warnings,
            plugin_info=plugin_info,
            general_messages=general_msgs,
        )
    finally:
        for _sym in _temp_data_symlinks:
            try:
                _sym.unlink(missing_ok=True)
            except OSError:
                pass


def find_overlapping_plugins(
    target_plugin: str,
    plugin_names: list[str],
    game_name: str,
    game_path: Path,
    staging_root: Path | None = None,
    log_fn=None,
    game_type_attr: str = "",
    game_data_dir: Path | None = None,
) -> list[str]:
    """Return plugins whose records overlap with `target_plugin`.

    Unlike sort, this requires a *full* plugin load (record bodies, not just
    headers) so libloot can compare FormIDs via Plugin.do_records_overlap.
    That load is comparatively expensive (hundreds of ms to seconds for large
    profiles), so callers should run this off the UI thread.

    Returns the overlapping plugin names (original casing, as found on disk),
    excluding the target itself. Raises RuntimeError if libloot is unavailable
    or no game type is configured.
    """
    _log = log_fn or (lambda _: None)

    if not _AVAILABLE:
        raise RuntimeError(
            "libloot is not available. "
            "Rebuild it with: ./LOOT/rebuild_libloot.sh"
        )
    if not game_type_attr:
        raise RuntimeError(
            f"No LOOT game type configured for '{game_name}'. "
            "Set loot_game_type in the game's Python handler."
        )
    game_type = getattr(loot.GameType, game_type_attr, None)
    if game_type is None:
        raise RuntimeError(
            f"Unknown libloot GameType '{game_type_attr}' for '{game_name}'."
        )

    # Deduplicate case-insensitively and ensure the target is included.
    seen_lower: set[str] = set()
    deduped: list[str] = []
    for n in plugin_names:
        nl = n.lower()
        if nl not in seen_lower:
            seen_lower.add(nl)
            deduped.append(n)
    if target_plugin.lower() not in seen_lower:
        deduped.append(target_plugin)
    plugin_names = deduped

    loot_data_dir = _get_data_dir()
    effective_data_dir = game_data_dir if game_data_dir is not None else game_path / "Data"

    _log("Initializing LOOT...")
    game = loot.Game(game_type, str(game_path), str(loot_data_dir))

    plugin_paths, missing = _find_plugin_paths(
        plugin_names, effective_data_dir, staging_root,
    )
    if not plugin_paths:
        return []

    _log(f"Loading {len(plugin_paths)} plugins (full records)...")
    game.load_plugins(plugin_paths)

    target = game.plugin(target_plugin)
    if target is None:
        raise RuntimeError(
            f"'{target_plugin}' could not be loaded (missing from disk?)."
        )

    overlapping: list[str] = []
    target_lower = target_plugin.lower()
    for name in plugin_names:
        if name.lower() == target_lower:
            continue
        other = game.plugin(name)
        if other is None:
            continue
        try:
            if target.do_records_overlap(other):
                overlapping.append(name)
        except Exception:
            continue

    _log(f"Found {len(overlapping)} overlapping plugin(s).")
    return overlapping
