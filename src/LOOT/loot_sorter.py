"""
loot_sorter.py
Sort a plugins.txt load order using libloot's masterlist-based sorting.

Usage:
    from LOOT.loot_sorter import sort_plugins, is_available

The sort_plugins() function takes the current plugin entries and game info,
feeds them through libloot's sorting algorithm, and returns the reordered
list with enabled/disabled state preserved.

Game support is driven by the game handler's properties:
  - loot_sort_enabled: bool — whether LOOT sorting applies to this game
  - loot_game_type: str     — libloot GameType attribute name (e.g. 'SkyrimSE')
  - loot_masterlist_url: str — URL to download the masterlist YAML
  - game_id: str            — used to derive the masterlist filename
                              (masterlist_<game_id>.yaml in ~/.config/AmethystModManager/LOOT/data/)
"""

from __future__ import annotations

import json
import shutil
import time
import urllib.request
from pathlib import Path
from dataclasses import dataclass, field

try:
    import LOOT.loot as loot
    _AVAILABLE = True
except ImportError:
    loot = None
    _AVAILABLE = False

# Bundled masterlists shipped with the application (read-only in AppImage)
_BUNDLED_DATA_DIR = Path(__file__).parent / "data"

# User-writable masterlist directory — resolved lazily to avoid import-time side effects
_DATA_DIR: Path | None = None

# Re-download masterlists at most once per 24 hours
_MASTERLIST_TTL_SECS = 86400


def _get_data_dir() -> Path:
    global _DATA_DIR
    if _DATA_DIR is None:
        from Utils.config_paths import get_loot_data_dir
        _DATA_DIR = get_loot_data_dir()
    return _DATA_DIR

PRELUDE_FILE = "masterlist_prelude.yaml"
PRELUDE_URL = "https://raw.githubusercontent.com/loot/prelude/v0.21/prelude.yaml"


def _ensure_masterlist(
    filename: str,
    download_url: str = "",
    log_fn=None,
) -> None:
    """Ensure a masterlist exists in the config dir, fetching if stale.

    Resolution order:
      1. If a cached file exists and is younger than _MASTERLIST_TTL_SECS, use it.
      2. Download from the provided URL.
         Falls back to the existing cached file if the download fails.
      3. If no URL or download fails and no cached file: copy from bundled data dir.
    """
    _log = log_fn or (lambda _: None)
    data_dir = _get_data_dir()
    dest = data_dir / filename

    # Skip download if the cached file is fresh enough
    if dest.is_file() and download_url:
        age = time.time() - dest.stat().st_mtime
        if age < _MASTERLIST_TTL_SECS:
            return

    # Try to download if a URL is provided
    if download_url:
        tmp = dest.with_suffix(".tmp")
        _log(f"Fetching latest {filename}...")
        try:
            urllib.request.urlretrieve(download_url, tmp)
            tmp.replace(dest)
            _log(f"Updated {filename}.")
            return
        except Exception as exc:
            _log(f"Could not fetch {filename}: {exc} — using cached copy.")
            if tmp.exists():
                tmp.unlink(missing_ok=True)

    # Fall through: use cached file if it exists
    if dest.is_file():
        return

    # Last resort: copy from bundled data dir
    src = _BUNDLED_DATA_DIR / filename
    if src.is_file():
        shutil.copy2(src, dest)
        _log(f"Copied bundled {filename} to config dir.")


def _masterlist_filename(game_type_attr: str) -> str:
    """Derive the masterlist filename from the libloot game type attribute name.
    e.g. 'SkyrimSE' → 'masterlist_skyrimse.yaml'
    """
    return f"masterlist_{game_type_attr.lower()}.yaml"


def is_available() -> bool:
    """Return True if libloot is importable and ready to use."""
    return _AVAILABLE


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
    # }
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


def _collect_plugin_info(
    db,
    plugin_names: list[str],
    language: str = "en",
) -> dict[str, dict]:
    """Collect evaluated masterlist/userlist metadata for each plugin.

    Returns a mapping of plugin name -> info dict (see SortResult.plugin_info).
    Only plugins that have at least one non-empty field are included.
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

        if not (messages or requirements or incompatibilities or locations):
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

    Schema v2: plugin_info values are dicts {messages, dirty, requirements,
    incompatibilities, locations}. Fields are omitted when empty.
    """
    profile_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": 2,
        "generated_at": int(time.time()),
        "game_id": game_id,
        "general_messages": general_messages,
        "plugins": plugin_info,
    }
    dest = profile_dir / "loot.json"
    tmp = dest.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(dest)


def read_loot_info(profile_dir: Path) -> dict:
    """Read loot.json from a profile dir. Returns empty dict on any failure."""
    path = profile_dir / "loot.json"
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _find_plugin_paths(
    plugin_names: list[str],
    game_data_dir: Path,
    staging_root: Path | None,
) -> tuple[list[str], list[str]]:
    """
    Locate plugin files on disk, searching the game's Data directory first,
    then falling back to the mod staging folders, then the overwrite folder.

    Returns:
        (found_paths, missing_names)
    """
    found: dict[str, str] = {}  # plugin name → absolute path
    # Track basenames already added to prevent duplicate filenames (libloot
    # requires every path to have a unique filename, case-insensitively).
    found_basenames: set[str] = set()
    names_lower = {n.lower(): n for n in plugin_names}

    # 1. Check the game's Data directory
    if game_data_dir.is_dir():
        for name in plugin_names:
            full = game_data_dir / name
            if full.is_file() and name.lower() not in found_basenames:
                found[name] = str(full)
                found_basenames.add(name.lower())

    # 2. For anything still missing, search staging mod folders (recursively)
    if staging_root and staging_root.is_dir():
        still_missing = [n for n in plugin_names if n not in found]
        if still_missing:
            missing_lower = {n.lower() for n in still_missing}
            for mod_dir in staging_root.iterdir():
                if not mod_dir.is_dir():
                    continue
                for f in mod_dir.rglob("*"):
                    if f.is_file() and f.name.lower() in missing_lower:
                        # Map back to the original-cased name
                        orig = names_lower.get(f.name.lower())
                        if orig and orig not in found and f.name.lower() not in found_basenames:
                            found[orig] = str(f)
                            found_basenames.add(f.name.lower())

    # 3. For anything still missing, check the overwrite folder
    #    (staging_root's sibling: Profiles/<game>/overwrite/)
    if staging_root:
        overwrite_dir = staging_root.parent / "overwrite"
        if overwrite_dir.is_dir():
            still_missing = [n for n in plugin_names if n not in found]
            if still_missing:
                missing_lower = {n.lower() for n in still_missing}
                for f in overwrite_dir.rglob("*"):
                    if f.is_file() and f.name.lower() in missing_lower:
                        orig = names_lower.get(f.name.lower())
                        if orig and orig not in found and f.name.lower() not in found_basenames:
                            found[orig] = str(f)
                            found_basenames.add(f.name.lower())

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
    _ensure_masterlist(ml_filename, download_url=masterlist_url, log_fn=_log)
    _ensure_masterlist(PRELUDE_FILE, download_url=PRELUDE_URL, log_fn=_log)

    loot_data_dir = _get_data_dir()
    masterlist_path = loot_data_dir / ml_filename
    prelude_path = loot_data_dir / PRELUDE_FILE

    if not masterlist_path.is_file():
        url_hint = (f"\nDownload from: {masterlist_url}" if masterlist_url
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
        names_needed = {n.lower() for n in plugin_names}
        # Build a map of lowercase plugin name → staging file path
        staging_plugin_map: dict[str, Path] = {}
        for mod_dir in staging_root.iterdir():
            if not mod_dir.is_dir():
                continue
            for f in mod_dir.rglob("*"):
                if (f.is_file()
                        and f.suffix.lower() in _plugin_exts
                        and f.name.lower() in names_needed
                        and f.name.lower() not in staging_plugin_map):
                    staging_plugin_map[f.name.lower()] = f

        for name_lower, src in staging_plugin_map.items():
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

        # Append any unsortable plugins (missing from disk) at the end
        if unsortable:
            sorted_names.extend(unsortable)

        # Count how many sortable plugins actually moved position
        moved = sum(
            1 for i, name in enumerate(sorted_names[:len(sortable)])
            if i >= len(sortable) or sortable[i] != name
        )

        _log(f"Sort complete. {moved} plugin(s) changed position.")

        # CRC-based filtering of dirty_info was tried but required a full
        # plugin-body load (game.load_plugins) that scales with total plugin
        # size — unacceptably slow for real profiles. We now skip CRC matching
        # and render every masterlist dirty entry; tooltips get noisier for
        # plugins with many known CRCs but sort stays fast.

        # Collect evaluated metadata for every plugin. Conditions are evaluated
        # against the plugin headers we just loaded, so this reflects the live
        # state of the current profile.
        try:
            plugin_info = _collect_plugin_info(db, sortable)
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
