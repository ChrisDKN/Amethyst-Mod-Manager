"""
Install mod from archive: extract, FOMOD wizard, strip/prefix, copy to staging, update modlist/plugins.
Used by ModListPanel, PluginPanel, TopBar, and App. Imports dialogs and mod_name_utils.
"""

import json
import os
import re
import shutil
import tarfile
import tempfile
import threading
import zipfile

# Ensures only one interactive dialog (FOMOD, Unexpected Mod Structure, etc.) is
# shown at a time when collection installs run parallel extraction workers.
# Any worker that needs user input acquires this lock, marshals the dialog to
# the main thread, waits for the result, then releases.
_interactive_dialog_lock = threading.Lock()

# Set while a FOMOD dialog is open (either on the main thread or via a worker).
# Used by the NXM install queue to defer new installs until the dialog closes,
# preventing a second FomodDialog overlay from being placed on top of the first.
fomod_dialog_active = threading.Event()

# Guards /tmp space accounting so parallel workers don't all race to claim the
# same free space before any of them has started writing.
_tmp_space_lock = threading.Lock()
_tmp_space_reserved: int = 0  # bytes currently claimed by in-flight extractions

# py7zr / liblzma is not safe to run in multiple threads simultaneously —
# concurrent extractions can segfault.  Serialize all py7zr calls globally.
_py7zr_lock = threading.Lock()
from pathlib import Path
from datetime import datetime

import py7zr

from gui.dialogs import (
    _ReplaceModDialog,
    _SelectFilesDialog,
    _SetPrefixDialog,
    queue_rename_after_install,
)
from gui.fomod_dialog import FomodDialog
from gui.mod_name_utils import _strip_title_metadata, _suggest_mod_names
from Utils.fomod_parser import detect_fomod, parse_module_config, parse_mod_info
from Utils.fomod_installer import resolve_files
from Utils.ui_config import load_dev_mode, load_rename_mod_after_install
from Utils.config_paths import get_fomod_selections_path
from Utils.plugins import read_plugins, append_plugin, read_loadorder, write_loadorder, PluginEntry
from Utils.modlist import prepend_mod, ensure_mod_preserving_position, read_modlist, write_modlist, ModEntry
from Utils.profile_state import read_separator_locks, write_separator_locks
from Utils.filemap import _scan_dir, update_mod_index
from Nexus.nexus_meta import write_meta, resolve_nexus_meta_for_archive
from gui.ctk_components import CTkNotification


def _run_dialog_on_main(parent_window, factory, result_holder: list,
                        done_event: threading.Event, result_attr: str | None = None) -> None:
    """Run on main thread via after(0, ...). Creates dialog via factory(parent), waits, stores result."""
    try:
        dlg = factory(parent_window)
        try:
            if dlg.winfo_exists():
                parent_window.wait_window(dlg)
        except Exception:
            pass
        result_holder[0] = getattr(dlg, result_attr) if result_attr else dlg
    except Exception:
        result_holder[0] = None
    finally:
        done_event.set()


def _show_replace_dialog_on_main(parent_window, mod_name: str,
                                 result_holder: list, done_event: threading.Event) -> None:
    _run_dialog_on_main(parent_window,
                        lambda p: _ReplaceModDialog(p, mod_name),
                        result_holder, done_event)


def _show_select_files_dialog_on_main(parent_window, file_list: list,
                                      result_holder: list, done_event: threading.Event) -> None:
    _run_dialog_on_main(parent_window,
                        lambda p: _SelectFilesDialog(p, file_list),
                        result_holder, done_event)


def _show_set_prefix_dialog_on_main(parent_window, required, file_list, mod_name: str,
                                    result_holder: list, done_event: threading.Event) -> None:
    _run_dialog_on_main(parent_window,
                        lambda p: _SetPrefixDialog(p, required, file_list, mod_name=mod_name),
                        result_holder, done_event, result_attr="result")


def _show_fomod_dialog_on_main(parent_window, config, mod_root,
                               installed_files: set, active_files: set,
                               saved_selections, selections_path,
                               result_holder: list, done_event: threading.Event) -> None:
    """Run on main thread. Creates a FomodDialog overlay on the mod-panel container."""
    import traceback as _tb
    try:
        container = getattr(parent_window, '_mod_panel_container', None) or parent_window

        def on_done(result):
            result_holder[0] = result
            done_event.set()

        panel = FomodDialog(container, config, mod_root,
                            installed_files=installed_files,
                            active_files=active_files,
                            saved_selections=saved_selections,
                            selections_path=selections_path,
                            on_done=on_done)
        try:
            if panel.winfo_exists():
                panel.place(relx=0, rely=0, relwidth=1, relheight=1)
                panel.lift()
                panel.focus_set()
        except Exception:
            _tb.print_exc()
    except Exception:
        _tb.print_exc()
        result_holder[0] = None
        done_event.set()


def _show_mod_notification(parent_window, message: str, state: str = "success") -> None:
    """Show a notification at bottom-right, auto-dismiss after 4 s.

    Always schedules on the main Tk thread: install_mod_from_archive is often
    invoked from a worker thread; creating CTkToplevel off-thread breaks
    geometry (e.g. square instead of the intended bar shape).
    """
    def _show():
        try:
            root = parent_window.winfo_toplevel()
            notif = CTkNotification(root, state=state, message=message)
            root.after(4000, notif.destroy)
        except Exception:
            pass

    try:
        parent_window.after(0, _show)
    except Exception:
        pass


def _build_tree_str(paths: list[str]) -> str:
    """Convert a flat list of slash-separated paths into an ASCII folder tree."""
    root: dict = {}
    for path in sorted(paths):
        node = root
        for part in path.split("/"):
            node = node.setdefault(part, {})

    lines: list[str] = []

    def _walk(node: dict, prefix: str):
        items = sorted(node.keys())
        for i, name in enumerate(items):
            is_last = (i == len(items) - 1)
            lines.append(f"{prefix}{'└── ' if is_last else '├── '}{name}")
            child = node[name]
            if child:
                _walk(child, prefix + ("    " if is_last else "│   "))

    _walk(root, "")
    return "\n".join(lines) if lines else "(no files)"


def _apply_strip_prefixes_to_file_list(
    file_list: list[tuple[str, str, bool]],
    strip_prefixes: set[str],
) -> list[tuple[str, str, bool]]:
    """
    Strip leading path segments from each dst_rel that match strip_prefixes
    (case-insensitive), repeatedly until the first segment is not in the set.
    """
    if not strip_prefixes:
        return file_list
    strip_lower = {p.lower() for p in strip_prefixes}
    result: list[tuple[str, str, bool]] = []
    for src_rel, dst_rel, is_folder in file_list:
        had_trailing = dst_rel.endswith("/") or dst_rel.endswith("\\")
        d = dst_rel.replace("\\", "/").strip("/")
        while "/" in d:
            first, remainder = d.split("/", 1)
            if first.lower() in strip_lower:
                d = remainder
            else:
                break
        if had_trailing and d:
            d = d + "/"
        result.append((src_rel, d, is_folder))
    return result


def _check_mod_top_level(file_list: list[tuple[str, str, bool]],
                         required: set[str]) -> bool:
    """Return True if at least one file's top-level folder matches a required name."""
    for _, dst_rel, _ in file_list:
        top = dst_rel.replace("\\", "/").split("/")[0].lower()
        if top in required:
            return True
    return False


def _try_auto_strip_top_level(
    file_list: list[tuple[str, str, bool]],
    required: set[str],
    max_strip_depth: int = 20,
) -> tuple[list[tuple[str, str, bool]], bool]:
    """
    Try stripping leading path segments until at least one file has a top-level
    folder in required. Returns (new_file_list, True) if a strip depth worked,
    otherwise (original file_list, False).
    """
    required_lower = {r.lower() for r in required}
    if _check_mod_top_level(file_list, required_lower):
        return (file_list, True)
    for strip_depth in range(1, max_strip_depth + 1):
        new_list: list[tuple[str, str, bool]] = []
        has_required = False
        for src_rel, dst_rel, is_folder in file_list:
            parts = dst_rel.replace("\\", "/").strip("/").split("/")
            if len(parts) <= strip_depth:
                continue
            new_dst = "/".join(parts[strip_depth:])
            top = parts[strip_depth].lower()
            if top in required_lower:
                has_required = True
            new_list.append((src_rel, new_dst, is_folder))
        if has_required and new_list:
            return (new_list, True)
    return (file_list, False)


def _check_mod_top_level_file_types(
    file_list: list[tuple[str, str, bool]],
    required_exts: set[str],
) -> bool:
    """Return True if at least one top-level file (no sub-folder) has a required extension."""
    exts_lower = {e.lower() for e in required_exts}
    for _, dst_rel, is_folder in file_list:
        if is_folder:
            continue
        dst_rel = dst_rel.replace("\\", "/").strip("/")
        if "/" not in dst_rel:
            ext = Path(dst_rel).suffix.lower()
            if ext in exts_lower:
                return True
    return False


def _try_auto_strip_for_file_types(
    file_list: list[tuple[str, str, bool]],
    required_exts: set[str],
    max_strip_depth: int = 20,
) -> tuple[list[tuple[str, str, bool]], bool]:
    """
    Try stripping leading path segments until at least one top-level file has a
    required extension.  Returns (new_file_list, True) if successful, otherwise
    (original file_list, False).
    """
    if _check_mod_top_level_file_types(file_list, required_exts):
        return (file_list, True)
    exts_lower = {e.lower() for e in required_exts}
    for strip_depth in range(1, max_strip_depth + 1):
        new_list: list[tuple[str, str, bool]] = []
        has_required = False
        for src_rel, dst_rel, is_folder in file_list:
            parts = dst_rel.replace("\\", "/").strip("/").split("/")
            if len(parts) <= strip_depth:
                continue
            new_dst = "/".join(parts[strip_depth:])
            if not is_folder and len(parts) == strip_depth + 1:
                ext = Path(new_dst).suffix.lower()
                if ext in exts_lower:
                    has_required = True
            new_list.append((src_rel, new_dst, is_folder))
        if has_required and new_list:
            return (new_list, True)
    return (file_list, False)


def _stamp_meta_install_date(meta_ini_path: Path, installation_file: str = "",
                              is_fomod: bool = False) -> None:
    """Write the current datetime as the ``installed`` key in meta.ini if not
    already present.  Also write ``installationFile`` if *installation_file* is
    given and the key is not yet set (MO2-compatible).  If *is_fomod* is True,
    also set ``FOMOD=True`` so the modlist panel can identify FOMOD installs."""
    import configparser as _cp
    parser = _cp.ConfigParser()
    if meta_ini_path.is_file():
        parser.read(str(meta_ini_path), encoding="utf-8")
    if not parser.has_section("General"):
        parser.add_section("General")
    changed = False
    if not parser.get("General", "installed", fallback=""):
        parser.set("General", "installed",
                   datetime.now().strftime("%Y-%m-%dT%H:%M:%S"))
        changed = True
    if installation_file and not parser.get("General", "installationFile", fallback=""):
        parser.set("General", "installationFile", installation_file)
        changed = True
    if is_fomod and parser.get("General", "FOMOD", fallback="").lower() != "true":
        parser.set("General", "FOMOD", "True")
        changed = True
    if changed:
        meta_ini_path.parent.mkdir(parents=True, exist_ok=True)
        with open(meta_ini_path, "w", encoding="utf-8") as fh:
            parser.write(fh)


def _expand_folders_for_dialog(
    file_list: list[tuple[str, str, bool]], src_root: str
) -> list[tuple[str, str, bool]]:
    """
    Expand any is_folder=True entries into individual file entries so the
    _SelectFilesDialog can show real files instead of opaque folder names.
    """
    result = []
    root = Path(src_root)
    for src_rel, dst_rel, is_folder in file_list:
        if not is_folder:
            result.append((src_rel, dst_rel, False))
            continue
        src_dir = root / src_rel if src_rel else root
        if not src_dir.is_dir():
            result.append((src_rel, dst_rel, True))  # fallback: keep as-is
            continue
        for entry in sorted(src_dir.rglob("*")):
            if entry.is_file():
                file_src_rel = str(entry.relative_to(root))
                rel_to_src = entry.relative_to(src_dir)
                file_dst_rel = str(Path(dst_rel) / rel_to_src) if dst_rel else str(rel_to_src)
                result.append((file_src_rel, file_dst_rel, False))
    return result


def _unwrap_single_folder(extract_dir: str) -> str:
    """If extract_dir contains exactly one subdirectory and no files, return
    that subdirectory's path.  Archives like 'ModName.zip' that contain a
    single top-level folder 'ModName/' would otherwise hide their contents
    from bundle/multi-mod detection."""
    root = Path(extract_dir)
    entries = list(root.iterdir())
    if len(entries) == 1 and entries[0].is_dir():
        return str(entries[0])
    return extract_dir


def detect_bundle(
    extract_dir: str,
) -> "tuple[str, list[tuple[str, str]]] | None":
    """Detect a Fluffy-style bundle: a directory whose immediate subdirs each
    contain a ``modinfo.ini`` with the same ``nameasbundle`` value.

    Returns ``(bundle_name, [(variant_name, variant_path), ...])`` sorted by
    the order subdirs appear on disk, or ``None`` if not a bundle.

    A bundle requires:
    - At least 2 immediate subdirectories.
    - Every subdir has a ``modinfo.ini`` with a non-empty ``nameasbundle`` key.
    - All ``nameasbundle`` values are identical.
    """
    import configparser
    root = Path(extract_dir)
    subdirs = sorted(p for p in root.iterdir() if p.is_dir())
    if len(subdirs) < 2:
        return None

    bundle_name: str | None = None
    variants: list[tuple[str, str]] = []

    for subdir in subdirs:
        ini_path = subdir / "modinfo.ini"
        if not ini_path.is_file():
            return None
        cfg = configparser.RawConfigParser()
        try:
            cfg.read_string("[mod]\n" + ini_path.read_text(encoding="utf-8", errors="replace"))
        except Exception:
            return None
        bname = cfg.get("mod", "nameasbundle", fallback="").strip()
        if not bname:
            return None
        if bundle_name is None:
            bundle_name = bname
        elif bname.lower() != bundle_name.lower():
            return None  # inconsistent bundle names
        vname = cfg.get("mod", "name", fallback=subdir.name).strip() or subdir.name
        variants.append((vname, str(subdir)))

    if bundle_name and len(variants) >= 2:
        return bundle_name, variants
    return None


def detect_multi_mod(
    extract_dir: str,
) -> "list[tuple[str, str]] | None":
    """Detect a multi-mod archive: immediate subdirs each have a ``modinfo.ini``
    but without a shared ``nameasbundle`` (so not a bundle).

    Returns ``[(mod_name, subdir_path), ...]`` or ``None`` if not applicable.
    Requires at least 2 subdirs all with modinfo.ini.
    """
    import configparser
    root = Path(extract_dir)
    subdirs = sorted(p for p in root.iterdir() if p.is_dir())
    if len(subdirs) < 2:
        return None
    mods: list[tuple[str, str]] = []
    for subdir in subdirs:
        ini_path = subdir / "modinfo.ini"
        if not ini_path.is_file():
            return None
        cfg = configparser.RawConfigParser()
        try:
            cfg.read_string("[mod]\n" + ini_path.read_text(encoding="utf-8", errors="replace"))
        except Exception:
            return None
        name = cfg.get("mod", "name", fallback="").strip() or subdir.name
        mods.append((name, str(subdir)))
    return mods if len(mods) >= 2 else None


def _resolve_direct_files(extract_dir: str) -> list[tuple[str, str, bool]]:
    """
    For a non-FOMOD archive, return every file as a (src, dst, is_folder)
    tuple where src and dst are both relative to the archive root.
    """
    result = []
    root = Path(extract_dir)
    for entry in root.rglob("*"):
        if entry.is_file():
            rel = str(entry.relative_to(root))
            result.append((rel, rel, False))
    return result


def _resolve_src_case(src_root: Path, src_rel: str,
                      _cache: "dict[Path, dict[str, str]] | None" = None) -> Path:
    """
    Build src_root / src_rel while resolving each path component case-insensitively
    against what actually exists on disk.  FOMOD XML is written on Windows (case-
    insensitive) so source paths like "00 SOS\\FULL\\" may not match the real
    capitalisation on a Linux filesystem (e.g. "00 SOS/Full/").

    Pass a shared dict as ``_cache`` to avoid re-scanning the same directories
    when resolving many paths under the same root.
    """
    if _cache is None:
        _cache = {}
    parts = src_rel.replace("\\", "/").strip("/").split("/")
    current = src_root
    for part in parts:
        if not part:
            continue
        if current not in _cache:
            try:
                _cache[current] = {p.name.lower(): p.name for p in current.iterdir() if current.is_dir()}
            except OSError:
                _cache[current] = {}
        resolved = _cache[current].get(part.lower(), part)
        current = current / resolved
    return current


def _resolve_dst_case(dest_root: Path, dst_rel: str,
                      _cache: "dict[Path, dict[str, str]] | None" = None) -> Path:
    """
    Build dest_root / dst_rel while resolving each path component case-insensitively
    against what already exists on disk.  This prevents FOMOD installs from creating
    duplicate folders that differ only in case (e.g. 'Interface' vs 'interface') when
    running on a case-sensitive Linux filesystem.

    Pass a shared dict as ``_cache`` to avoid re-scanning the same directories
    when resolving many paths under the same root.
    """
    if _cache is None:
        _cache = {}
    parts = dst_rel.replace("\\", "/").split("/")
    current = dest_root
    for part in parts:
        if not part:
            continue
        if current not in _cache:
            try:
                _cache[current] = {p.name.lower(): p.name for p in current.iterdir() if current.is_dir()}
            except OSError:
                _cache[current] = {}
        resolved = _cache[current].get(part.lower(), part)
        current = current / resolved
    return current


def _link_or_copy(src: str | os.PathLike, dst: str | os.PathLike) -> None:
    """
    Place src at dst using a hardlink when the two are on the same filesystem,
    otherwise fall back to a byte-for-byte copy.  Hardlinks are near-instant
    and cost zero additional disk space, which roughly halves install time
    (and peak disk usage) for large archives that get extracted next to the
    staging folder.  Cross-device (EXDEV) and filesystems that don't support
    hardlinks (ENOSYS/EPERM) transparently fall through to shutil.copy2, so
    the small-archive tmpfs path behaves exactly as before.
    """
    try:
        os.link(src, dst)
        return
    except OSError:
        # EXDEV (cross-device), ENOSYS / EPERM (fs doesn't support hardlinks),
        # or any other link-time failure — fall back to a real copy.
        shutil.copy2(src, dst)


def _copytree_case_insensitive(src: Path, dst: Path) -> int:
    """
    Recursively copy src into dst, resolving every destination directory
    component case-insensitively against what already exists on disk.
    This is a case-aware replacement for shutil.copytree(dirs_exist_ok=True).
    Returns the number of files copied.
    """
    copied = 0
    dst.mkdir(parents=True, exist_ok=True)
    for entry in os.scandir(src):
        # Resolve this entry's name against what already exists in dst
        try:
            existing = {p.name.lower(): p.name for p in dst.iterdir()}
        except OSError:
            existing = {}
        resolved_name = existing.get(entry.name.lower(), entry.name)
        child_dst = dst / resolved_name
        if entry.is_dir(follow_symlinks=False):
            copied += _copytree_case_insensitive(Path(entry.path), child_dst)
        elif entry.is_file(follow_symlinks=False):
            child_dst.parent.mkdir(parents=True, exist_ok=True)
            if child_dst.is_dir():
                shutil.rmtree(child_dst)
            elif child_dst.exists():
                child_dst.chmod(0o644)
                child_dst.unlink()
            _link_or_copy(entry.path, child_dst)
            copied += 1
    return copied


def _copy_file_list(file_list: list[tuple[str, str, bool]],
                    src_root: str, dest_root: Path, log_fn) -> None:
    """
    Copy each (source, destination, is_folder) entry from src_root into dest_root.
    Folder entries use the recursive case-insensitive copytree (serial).
    File-only entries are copied in parallel for speed on large mods.
    """
    from concurrent.futures import ThreadPoolExecutor
    import threading as _threading

    folder_copied = 0
    file_entries: list[tuple[Path, Path]] = []  # (src, dst) pairs ready to copy

    _src_cache: dict[Path, dict[str, str]] = {}
    _dst_cache: dict[Path, dict[str, str]] = {}
    _src_root_path = Path(src_root)

    for src_rel, dst_rel, is_folder in file_list:
        # For direct installs src_rel == dst_rel and the path was produced by rglob
        # on the local filesystem — no case resolution needed on the source side.
        if src_rel and src_rel == dst_rel:
            src = _src_root_path / src_rel.replace("\\", "/")
        elif src_rel:
            src = _resolve_src_case(_src_root_path, src_rel, _src_cache)
        else:
            src = _src_root_path

        dst = _resolve_dst_case(dest_root, dst_rel, _dst_cache) if dst_rel else dest_root / dst_rel

        if is_folder:
            if not dst_rel:
                dst = dest_root
            if src.is_dir():
                folder_copied += _copytree_case_insensitive(src, dst)
                # Invalidate the cache for dst's parent so that subsequent
                # path resolutions pick up directories just created above.
                _dst_cache.pop(dst.parent, None)
        else:
            if not dst_rel:
                dst = dest_root / src.name
            elif dst_rel.endswith("/") or dst_rel.endswith("\\"):
                dst = _resolve_dst_case(dest_root, dst_rel.rstrip("/\\"), _dst_cache) / src.name
            if src.is_file():
                file_entries.append((src, dst))

    # Pre-create all destination directories (serial — avoids mkdir races).
    dirs_seen: set[Path] = set()
    for _, dst in file_entries:
        d = dst.parent
        if d not in dirs_seen:
            d.mkdir(parents=True, exist_ok=True)
            dirs_seen.add(d)

    _copy_count = _threading.local()

    def _copy_one(src_dst: tuple[Path, Path]) -> None:
        src, dst = src_dst
        if dst.is_dir():
            shutil.rmtree(dst)
        elif dst.exists() or dst.is_symlink():
            # Unlink first so we don't punch through a hardlink shared with
            # another mod's staging file (deploy uses hardlinks on this setup).
            try:
                dst.unlink()
            except PermissionError:
                dst.chmod(0o644)
                dst.unlink()
        _link_or_copy(src, dst)

    _COPY_WORKERS = 8
    with ThreadPoolExecutor(max_workers=_COPY_WORKERS) as pool:
        # map() streams results lazily — far less memory overhead than submit()+as_completed()
        # when file_entries is large (tens of thousands of files).
        for _ in pool.map(_copy_one, file_entries, chunksize=256):
            pass

    copied = folder_copied + len(file_entries)
    log_fn(f"Copied {copied} item(s) to staging area.")


FOMOD_DEFERRED = "__FOMOD_DEFERRED__"


def _fire_on_installed(cb, is_fomod: bool = False) -> None:
    """Invoke an `on_installed` callback with backwards-compatible signature.

    Callers may supply either a no-arg callable or one that accepts an
    `is_fomod` keyword. Try the richer form first, then fall back."""
    if cb is None:
        return
    try:
        cb(is_fomod=is_fomod)
        return
    except TypeError:
        pass
    except Exception:
        return
    try:
        cb()
    except Exception:
        pass


def _maybe_queue_rename_after_install(parent_window, mod_panel, headless: bool,
                                      mod_name: str, suggestions: list[str]) -> None:
    """Schedule a post-install rename prompt if the option is enabled.

    Collection installs pass ``headless=True`` and are deliberately skipped.
    The queue helper ensures only one dialog is visible at a time even when
    parallel workers finish close together.
    """
    if headless or mod_panel is None or parent_window is None or not mod_name:
        return
    try:
        if not load_rename_mod_after_install():
            return
    except Exception:
        return
    try:
        queue_rename_after_install(parent_window, mod_panel, mod_name, suggestions)
    except Exception:
        pass


def install_mod_from_archive(archive_path: str, parent_window, log_fn,
                             game, mod_panel=None,
                             on_installed=None,
                             fomod_auto_selections: "dict | None" = None,
                             prebuilt_meta=None,
                             disable_extract: bool = False,
                             profile_dir=None,
                             headless: bool = False,
                             preferred_name: str = "",
                             skip_index_update: bool = False,
                             overwrite_existing: "bool | None" = None,
                             progress_fn=None,
                             clear_progress_fn=None,
                             defer_interactive_fomod: bool = False) -> None:
    """
    Extract archive to a temp directory, detect FOMOD, run the wizard if
    present, then copy the resolved files into the game's mod staging area.
    Supports .zip, .7z, and .tar.* formats.

    on_installed : optional callable
        Called after a successful install, before the function returns.
        Use this to e.g. delete the source archive or refresh the UI.
        May be defined as either ``f()`` or ``f(is_fomod: bool)`` —
        when a keyword ``is_fomod`` is accepted, it indicates the mod
        was installed via a FOMOD installer.

    fomod_auto_selections : dict | None
        When provided, the FOMOD wizard is skipped entirely and these
        pre-resolved selections are passed straight to ``resolve_files()``.
        Format: ``{step_name: {group_name: [plugin_name, ...]}}``
        (same structure as ``saved_selections`` / ``FomodDialog.result``).
        Intended for collection installs where the author has already
        chosen the FOMOD options.

    disable_extract : bool
        When True, skip extraction entirely.  The archive file is moved as-is
        into the mod staging folder (inside a folder named after the archive
        stem) instead of being extracted.  Useful for games that expect mods
        to remain in zip/archive format.
    """
    ext = archive_path.lower()
    raw_stem = os.path.splitext(os.path.basename(archive_path))[0]
    if raw_stem.endswith(".tar"):
        raw_stem = os.path.splitext(raw_stem)[0]

    suggestions = _suggest_mod_names(raw_stem)
    mod_name = preferred_name.strip() if preferred_name.strip() else (suggestions[0] if suggestions else raw_stem)

    # ------------------------------------------------------------------
    # Disable-extract mode: move the archive as-is into the mod folder.
    # ------------------------------------------------------------------
    if disable_extract:
        try:
            dest_root = game.get_effective_mod_staging_path() / mod_name
            was_existing_mod = dest_root.exists()
            if dest_root.exists():
                if threading.current_thread() is threading.main_thread():
                    replace_dialog = _ReplaceModDialog(parent_window, mod_name)
                    parent_window.wait_window(replace_dialog)
                else:
                    with _interactive_dialog_lock:
                        _rh: list = [None]
                        _ev = threading.Event()
                        parent_window.after(0, lambda: _show_replace_dialog_on_main(parent_window, mod_name, _rh, _ev))
                        _ev.wait()
                        replace_dialog = _rh[0]
                if replace_dialog is None or replace_dialog.result == "cancel":
                    log_fn(f"Install cancelled — '{mod_name}' already exists.")
                    return
                if replace_dialog.result == "rename":
                    mod_name = replace_dialog.new_name
                    dest_root = game.get_effective_mod_staging_path() / mod_name
                    was_existing_mod = False
                elif replace_dialog.result == "all":
                    def _force_remove(func, path, _exc):
                        os.chmod(path, 0o700)
                        func(path)
                    shutil.rmtree(dest_root, onexc=_force_remove)
            dest_root.mkdir(parents=True, exist_ok=True)
            archive_filename = os.path.basename(archive_path)
            dest_file = dest_root / archive_filename
            if dest_file.exists():
                dest_file.unlink()
            shutil.copy2(archive_path, dest_file)
            log_fn(f"Installed '{mod_name}' (no extract) → {dest_root}")

            _stamp_meta_install_date(dest_root / "meta.ini",
                                     installation_file=archive_filename)

            _ne_meta_path = dest_root / "meta.ini"
            _ne_archive = Path(archive_path)
            _ne_game_domain = getattr(game, "nexus_game_domain", "")
            if prebuilt_meta is not None:
                try:
                    prebuilt_meta.installation_file = archive_filename
                    write_meta(_ne_meta_path, prebuilt_meta)
                except OSError:
                    pass
            elif _ne_game_domain and _ne_archive.is_file():
                def _detect_meta_no_extract():
                    try:
                        app = None
                        try:
                            app = parent_window.winfo_toplevel()
                        except Exception:
                            pass
                        api = getattr(app, "_nexus_api", None) if app else None
                        meta = resolve_nexus_meta_for_archive(
                            _ne_archive, _ne_game_domain,
                            api=api,
                            log_fn=lambda m: (
                                app.after(0, lambda msg=m: log_fn(msg))
                                if app else None
                            ),
                        )
                        if meta:
                            write_meta(_ne_meta_path, meta)
                            msg = f"Nexus: Saved metadata for '{mod_name}' (mod {meta.mod_id})"
                            if app:
                                app.after(0, lambda: log_fn(msg))
                    except Exception:
                        pass
                threading.Thread(target=_detect_meta_no_extract, daemon=True).start()

            if mod_panel is not None and mod_panel._modlist_path is not None:
                modlist_path = mod_panel._modlist_path
            else:
                profile_dir = game.get_profile_root() / "profiles" / "default"
                modlist_path = profile_dir / "modlist.txt"

            if was_existing_mod:
                ensure_mod_preserving_position(modlist_path, mod_name, enabled=True)
            else:
                prepend_mod(modlist_path, mod_name, enabled=True)

            log_fn(f"Added '{mod_name}' to modlist.")
            _show_mod_notification(parent_window, f"Installed: {mod_name}")

            # Disable-extract path never runs a FOMOD installer.
            _fire_on_installed(on_installed, is_fomod=False)

            if mod_panel is not None:
                mod_panel.after(0, mod_panel.reload_after_install)
            _maybe_queue_rename_after_install(
                parent_window, mod_panel, headless=False,
                mod_name=mod_name, suggestions=suggestions,
            )
            return mod_name
        except Exception as e:
            import traceback
            log_fn(f"Install error: {e}")
            log_fn(traceback.format_exc())
        return None

    # Use /tmp (tmpfs) only when it has enough headroom for this archive plus a
    # 512 MB safety margin.  A module-level lock + reservation counter prevents
    # parallel workers from all racing to claim the same free space before any
    # of them has started writing.
    # We query the real uncompressed size from archive metadata so that archives
    # with extreme compression ratios (e.g. 700 MB → 8 GB texture mods) don't
    # overflow /tmp.  A fallback multiplier is used only when metadata is
    # unavailable (e.g. solid .7z archives where 7z listing is slow/unavailable).
    global _tmp_space_reserved
    try:
        _archive_size = os.path.getsize(archive_path)
    except OSError:
        _archive_size = 0

    def _get_uncompressed_size(path: str, compressed_size: int) -> int:
        """Return best-effort total uncompressed size of the archive in bytes."""
        _ext = path.lower()
        # ZIP: fast metadata read via zipfile
        if _ext.endswith(".zip"):
            try:
                with zipfile.ZipFile(path, "r") as _zf:
                    _total = sum(m.file_size for m in _zf.infolist())
                if _total > 0:
                    return _total
            except Exception:
                pass
        # 7z/rar/zip fallback: use `7z l -slt` which prints Size: per entry
        _7z_bin = shutil.which("7zzs") or shutil.which("7z") or shutil.which("7za")
        if _7z_bin:
            try:
                import subprocess
                _res = subprocess.run(
                    [_7z_bin, "l", "-slt", path],
                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                    text=True, timeout=30,
                )
                _total = 0
                for _line in _res.stdout.splitlines():
                    if _line.startswith("Size = "):
                        try:
                            _total += int(_line.split("=", 1)[1].strip())
                        except ValueError:
                            pass
                if _total > 0:
                    return _total
            except Exception:
                pass
        # Fallback: assume a generous 15× expansion (handles extreme texture packs)
        return compressed_size * 15

    _extract_size_estimate = _get_uncompressed_size(archive_path, _archive_size)
    _staging = game.get_effective_mod_staging_path()
    _tmp_claimed = False
    with _tmp_space_lock:
        try:
            _tmp_stat = os.statvfs("/tmp")
            _tmp_free = _tmp_stat.f_frsize * _tmp_stat.f_bavail
            _tmp_headroom = 512 * 1024 * 1024  # keep 512 MB free in /tmp
            _use_tmp = _extract_size_estimate + _tmp_headroom + _tmp_space_reserved < _tmp_free
        except OSError:
            _use_tmp = False
        if _use_tmp:
            _tmp_space_reserved += _extract_size_estimate
            _tmp_claimed = True
    if _use_tmp:
        _tmp_parent = None  # let mkdtemp use the default /tmp
    else:
        _tmp_parent = _staging.parent if _staging else None
    try:
        if _tmp_parent:
            _tmp_parent.mkdir(parents=True, exist_ok=True)
        extract_dir = tempfile.mkdtemp(prefix="modmgr_", dir=_tmp_parent or None)
    except OSError:
        extract_dir = tempfile.mkdtemp(prefix="modmgr_")

    try:
        if not os.path.isfile(archive_path):
            raise FileNotFoundError(
                f"Archive not found (may have been deleted after a prior install): "
                f"'{os.path.basename(archive_path)}'"
            )

        if ext.endswith(".zip"):
            import subprocess
            _zip_done = False
            # For large ZIPs, prefer native tools (7z or bsdtar) over Python's
            # single-threaded zipfile — they use C/multi-threaded extraction.
            _archive_mb = os.path.getsize(archive_path) / (1024 * 1024)
            _7z_bin = shutil.which("7zzs") or shutil.which("7z") or shutil.which("7za")
            _bsdtar_bin = shutil.which("bsdtar")
            _has_native = _7z_bin or _bsdtar_bin
            # Python's zipfile is pure-Python and single-threaded, so use it
            # only for very small archives (where native-tool subprocess
            # overhead dominates) or when no native tool is available.
            _use_python_zip = _archive_mb < 10 or not _has_native
            if _use_python_zip:
                try:
                    log_fn("Extracting with zipfile…")
                    with zipfile.ZipFile(archive_path, "r") as z:
                        members = z.infolist()
                        # Normalise Windows backslash paths so Linux extractors
                        # create the correct folder hierarchy instead of treating
                        # the whole path as a single filename.
                        _has_backslash = any("\\" in m.filename for m in members)
                        if _has_backslash:
                            for m in members:
                                m.filename = m.filename.replace("\\", "/")
                        if progress_fn is not None:
                            progress_fn(0, 0, "Extracting…")
                        z.extractall(extract_dir, members)
                    _zip_done = True
                except Exception as e_zip:
                    log_fn(f"zipfile failed ({e_zip}), retrying with native tools…")
            if not _zip_done and _7z_bin:
                shutil.rmtree(extract_dir, ignore_errors=True)
                os.makedirs(extract_dir, exist_ok=True)
                if progress_fn is not None:
                    progress_fn(0, 0, "Extracting…")
                result = subprocess.run(
                    [_7z_bin, "x", archive_path, f"-o{extract_dir}", "-y", "-mmt=on"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
                )
                if result.returncode == 0:
                    _zip_done = True
                    log_fn("Extracted with 7z.")
                else:
                    log_fn(f"7z failed ({result.stderr.strip()}), retrying with bsdtar…")
            if not _zip_done and _bsdtar_bin:
                shutil.rmtree(extract_dir, ignore_errors=True)
                os.makedirs(extract_dir, exist_ok=True)
                if progress_fn is not None:
                    progress_fn(0, 0, "Extracting…")
                result = subprocess.run(
                    [_bsdtar_bin, "-xf", archive_path, "-C", extract_dir],
                    stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
                )
                if result.returncode == 0:
                    _zip_done = True
                    log_fn("Extracted with bsdtar.")
                else:
                    log_fn(f"bsdtar failed ({result.stderr.strip()}).")
            if not _zip_done:
                # Last resort: Python zipfile if native tools failed or weren't tried
                try:
                    shutil.rmtree(extract_dir, ignore_errors=True)
                    os.makedirs(extract_dir, exist_ok=True)
                    log_fn("Extracting with zipfile (fallback)…")
                    with zipfile.ZipFile(archive_path, "r") as z:
                        members = z.infolist()
                        if any("\\" in m.filename for m in members):
                            for m in members:
                                m.filename = m.filename.replace("\\", "/")
                        if progress_fn is not None:
                            progress_fn(0, 0, "Extracting…")
                        z.extractall(extract_dir, members)
                    _zip_done = True
                except Exception as e_zip2:
                    raise RuntimeError(f"All extraction methods failed for ZIP: {e_zip2}")
        elif ext.endswith(".7z"):
            import subprocess
            _7z_done = False
            _archive_bytes = os.path.getsize(archive_path)
            _archive_mb = _archive_bytes / (1024 * 1024)
            # Prefer native 7z binary (multi-threaded) or bsdtar (native C)
            # over py7zr which is single-threaded and globally serialized.
            _7z_bin = shutil.which("7zzs") or shutil.which("7z") or shutil.which("7za")
            _bsdtar_bin = shutil.which("bsdtar")
            if _7z_bin:
                if progress_fn is not None:
                    progress_fn(0, 0, "Extracting…")
                result = subprocess.run(
                    [_7z_bin, "x", archive_path, f"-o{extract_dir}", "-y", "-mmt=on"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
                )
                if result.returncode == 0:
                    _7z_done = True
                    log_fn("Extracted with 7z.")
                else:
                    log_fn(f"7z failed ({result.stderr.strip()}), trying next method…")
            if not _7z_done and _bsdtar_bin:
                shutil.rmtree(extract_dir, ignore_errors=True)
                os.makedirs(extract_dir, exist_ok=True)
                if progress_fn is not None:
                    progress_fn(0, 0, "Extracting…")
                result = subprocess.run(
                    [_bsdtar_bin, "-xf", archive_path, "-C", extract_dir],
                    stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
                )
                if result.returncode == 0:
                    _7z_done = True
                    log_fn("Extracted with bsdtar.")
                else:
                    log_fn(f"bsdtar failed ({result.stderr.strip()}), trying py7zr…")
            if not _7z_done:
                # Fallback to py7zr — single-threaded and serialized, but works
                # when no native tools are available.
                # Check available RAM; keep a 512 MB safety margin.
                try:
                    with open("/proc/meminfo") as _mf:
                        for _line in _mf:
                            if _line.startswith("MemAvailable:"):
                                _avail_mb = int(_line.split()[1]) / 1024
                                break
                        else:
                            _avail_mb = 512.0
                except OSError:
                    _avail_mb = 512.0
                _py7zr_safe = _archive_mb < (_avail_mb - 512)
                if _py7zr_safe:
                    try:
                        log_fn("Extracting with py7zr…")
                        if progress_fn is not None:
                            progress_fn(0, 0, "Extracting…")
                        with _py7zr_lock:
                            with py7zr.SevenZipFile(archive_path, "r") as z:
                                z.extractall(extract_dir)
                        _7z_done = True
                    except Exception as e7:
                        log_fn(f"py7zr failed ({e7}).")
                else:
                    log_fn(f"Archive is {_archive_mb:.0f} MB, only {_avail_mb:.0f} MB RAM available — skipping py7zr to avoid OOM.")
            if not _7z_done:
                raise RuntimeError(f"All extraction methods failed for 7z archive.")
        elif any(ext.endswith(s) for s in (".tar.gz", ".tar.bz2", ".tar.xz", ".tar")):
            import subprocess
            _tar_done = False
            # Prefer native multi-threaded tools over Python's single-threaded
            # tarfile module — especially important for .tar.xz which is slow
            # to decompress in pure Python.
            _7z_bin = shutil.which("7zzs") or shutil.which("7z") or shutil.which("7za")
            _bsdtar_bin = shutil.which("bsdtar")
            if _bsdtar_bin:
                # bsdtar handles every tar variant natively and is the fastest
                # option for streaming tar decompression.
                log_fn("Extracting with bsdtar…")
                if progress_fn is not None:
                    progress_fn(0, 0, "Extracting…")
                result = subprocess.run(
                    [_bsdtar_bin, "-xf", archive_path, "-C", extract_dir],
                    stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
                )
                if result.returncode == 0:
                    _tar_done = True
                    log_fn("Extracted with bsdtar.")
                else:
                    log_fn(f"bsdtar failed ({result.stderr.strip()}), trying 7z…")
            if not _tar_done and _7z_bin:
                # 7z needs two passes for .tar.gz/.tar.xz/.tar.bz2 (decompress,
                # then untar).  For a plain .tar a single pass suffices.
                if ext.endswith(".tar"):
                    shutil.rmtree(extract_dir, ignore_errors=True)
                    os.makedirs(extract_dir, exist_ok=True)
                    if progress_fn is not None:
                        progress_fn(0, 0, "Extracting…")
                    result = subprocess.run(
                        [_7z_bin, "x", archive_path, f"-o{extract_dir}", "-y", "-mmt=on"],
                        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
                    )
                    if result.returncode == 0:
                        _tar_done = True
                        log_fn("Extracted with 7z.")
                    else:
                        log_fn(f"7z failed ({result.stderr.strip()}), trying tarfile…")
                else:
                    # Decompress to an intermediate .tar inside extract_dir,
                    # then untar that into extract_dir and remove the .tar.
                    shutil.rmtree(extract_dir, ignore_errors=True)
                    os.makedirs(extract_dir, exist_ok=True)
                    if progress_fn is not None:
                        progress_fn(0, 0, "Extracting…")
                    _stage_dir = tempfile.mkdtemp(prefix="modmgr_tar_",
                                                  dir=os.path.dirname(extract_dir) or None)
                    try:
                        r1 = subprocess.run(
                            [_7z_bin, "x", archive_path, f"-o{_stage_dir}", "-y", "-mmt=on"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
                        )
                        if r1.returncode != 0:
                            log_fn(f"7z decompress failed ({r1.stderr.strip()}), trying tarfile…")
                        else:
                            _inner_tar = None
                            for _n in os.listdir(_stage_dir):
                                if _n.lower().endswith(".tar"):
                                    _inner_tar = os.path.join(_stage_dir, _n)
                                    break
                            if _inner_tar:
                                r2 = subprocess.run(
                                    [_7z_bin, "x", _inner_tar, f"-o{extract_dir}", "-y", "-mmt=on"],
                                    stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
                                )
                                if r2.returncode == 0:
                                    _tar_done = True
                                    log_fn("Extracted with 7z.")
                                else:
                                    log_fn(f"7z untar failed ({r2.stderr.strip()}), trying tarfile…")
                    finally:
                        shutil.rmtree(_stage_dir, ignore_errors=True)
            if not _tar_done:
                shutil.rmtree(extract_dir, ignore_errors=True)
                os.makedirs(extract_dir, exist_ok=True)
                log_fn("Extracting with tarfile (fallback)…")
                if progress_fn is not None:
                    progress_fn(0, 0, "Extracting…")
                with tarfile.open(archive_path, "r:*") as t:
                    t.extractall(extract_dir, filter="fully_trusted")
        elif ext.endswith(".rar"):
            import subprocess
            _rar_done = False
            try:
                import rarfile
                log_fn("Extracting with rarfile…")
                if progress_fn is not None:
                    progress_fn(0, 0, "Extracting…")
                with rarfile.RarFile(archive_path, "r") as r:
                    r.extractall(extract_dir)
                _rar_done = True
            except ImportError:
                pass
            except Exception as e_rar:
                log_fn(f"rarfile failed ({e_rar}), trying next method…")
            if not _rar_done and shutil.which("unrar"):
                log_fn("Extracting with unrar…")
                if progress_fn is not None:
                    progress_fn(0, 0, "Extracting…")
                result = subprocess.run(
                    ["unrar", "x", "-y", archive_path, extract_dir + os.sep],
                    stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
                )
                if result.returncode != 0:
                    log_fn(f"unrar failed ({result.stderr.strip()}), trying bsdtar…")
                else:
                    _rar_done = True
            if not _rar_done:
                log_fn("Extracting with bsdtar…")
                if progress_fn is not None:
                    progress_fn(0, 0, "Extracting…")
                result = subprocess.run(
                    ["bsdtar", "-xf", archive_path, "-C", extract_dir],
                    stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
                )
                if result.returncode != 0:
                    raise RuntimeError(f"bsdtar failed: {result.stderr.strip()}")
                log_fn("Extracted with bsdtar.")
        else:
            log_fn(f"Unsupported archive format: {os.path.basename(archive_path)}")
            log_fn("Supported formats: .zip, .7z, .rar, .tar.gz")
            return None

        is_fomod_install = False
        fomod_result = detect_fomod(extract_dir)
        if fomod_result:
            mod_root, config_path = fomod_result
            config = parse_module_config(config_path)

            # info.xml <Name> is set by the mod author to the specific variant
            # name and is more reliable than ModuleConfig.xml <moduleName>,
            # which mod authors sometimes copy from a sibling variant and forget
            # to update.  Prefer info.xml when it exists and differs.
            _info_path = str(Path(config_path).parent / "info.xml")
            _mod_info = parse_mod_info(_info_path)
            if _mod_info.name and _mod_info.name != config.name:
                config.name = _mod_info.name

            if config.name:
                fomod_clean = _strip_title_metadata(config.name)
                seen = set()
                new_suggestions = []
                for c in (fomod_clean, config.name, *suggestions):
                    if c and c not in seen:
                        seen.add(c)
                        new_suggestions.append(c)
                suggestions = new_suggestions
                # Only let the FOMOD name override mod_name when the caller
                # didn't supply an explicit preferred_name (collection installs
                # use preferred_name to keep mods from the same page distinct).
                # Also skip the override when the filename already encodes a
                # specific variant name that differs from the FOMOD moduleName.
                # Example: file = "Security Overhaul SKSE - Regional Locks-…"
                #          FOMOD moduleName = "Security Overhaul SKSE - Add-ons"
                # Both share a common prefix but diverge — using the FOMOD name
                # would install into the wrong folder and load saved selections
                # from a different variant.
                # Rule: don't let the FOMOD name override when the filename name
                # and FOMOD name share a substantial common prefix but then
                # diverge (i.e. they are sibling variants, not the same thing).
                _orig_nexus_clean = re.sub(r"(-\d+)+$", "", raw_stem).strip()
                _filename_lower = _orig_nexus_clean.lower()
                _fomod_lower = fomod_clean.lower()
                # Find length of shared prefix (word-boundary aware: tokenize
                # on any non-alphanumeric run so punctuation-separated variants
                # like "Foo-Bar_Regional" vs "Foo-Bar_AddOns" split correctly).
                _fn_words = [w for w in re.split(r"[^a-z0-9]+", _filename_lower) if w]
                _fm_words = [w for w in re.split(r"[^a-z0-9]+", _fomod_lower) if w]
                _shared = 0
                for _a, _b in zip(_fn_words, _fm_words):
                    if _a == _b:
                        _shared += 1
                    else:
                        break
                # They are sibling variants if: they share words, but both have
                # additional words that differ from each other.
                _are_siblings = (
                    _shared > 0
                    and _shared < len(_fn_words)
                    and _shared < len(_fm_words)
                    and _fn_words[_shared:] != _fm_words[_shared:]
                )
                if not preferred_name.strip() and not _are_siblings:
                    mod_name = suggestions[0]

            installed_files: set[str] = set()
            active_files: set[str] = set()
            _plugins_dir = None
            if mod_panel is not None and getattr(mod_panel, "_modlist_path", None) is not None:
                _plugins_dir = mod_panel._modlist_path.parent
            elif profile_dir is not None:
                from pathlib import Path as _Path
                _plugins_dir = _Path(profile_dir)
            if _plugins_dir is not None:
                plugins_path = _plugins_dir / "plugins.txt"
                loadorder_path = _plugins_dir / "loadorder.txt"
                for entry in read_plugins(plugins_path):
                    installed_files.add(entry.name.lower())
                    if entry.enabled:
                        active_files.add(entry.name.lower())
                # Also add anything in loadorder.txt not already captured
                for name in read_loadorder(loadorder_path):
                    installed_files.add(name.lower())
            # Add vanilla/DLC plugins from the game's Data directory. These are
            # loaded implicitly by the engine and never appear in plugins.txt
            _vanilla_path = None
            if game is not None:
                try:
                    _vanilla_path = game.get_vanilla_plugins_path()
                except Exception:
                    pass
            if _vanilla_path is not None:
                try:
                    _plugin_exts = {".esm", ".esl", ".esp"}
                    for _vf in _vanilla_path.iterdir():
                        if _vf.suffix.lower() in _plugin_exts:
                            _vname = _vf.name.lower()
                            installed_files.add(_vname)
                            active_files.add(_vname)
                except Exception:
                    pass

            if fomod_auto_selections is None and defer_interactive_fomod:
                # Collection install: no auto-selections available — defer this
                # mod until all non-FOMOD mods have installed so its dependencies
                # are present before the user is prompted.
                log_fn("FOMOD installer detected — deferring until dependencies are installed.")
                return FOMOD_DEFERRED

            def _write_profile_fomod(selections):
                """Mirror fomod selections into <profile>/fomod/<mod>.json."""
                pdir = getattr(game, "_active_profile_dir", None)
                if not pdir:
                    return
                try:
                    pfomod = Path(pdir) / "fomod"
                    pfomod.mkdir(parents=True, exist_ok=True)
                    with open(pfomod / f"{mod_name}.json", "w", encoding="utf-8") as f:
                        json.dump(selections, f, indent=2)
                except OSError:
                    pass

            if fomod_auto_selections is not None:
                # Collection install: use the author's pre-chosen options,
                # skip the interactive wizard entirely.
                log_fn("FOMOD installer detected — applying collection author's choices automatically.")
                final_selections = fomod_auto_selections
                game_name = getattr(game, "name", "")
                if game_name:
                    sel_path = get_fomod_selections_path(game_name, mod_name)
                    try:
                        with open(sel_path, "w", encoding="utf-8") as f:
                            json.dump(final_selections, f, indent=2)
                    except OSError:
                        pass
                _write_profile_fomod(final_selections)
            else:
                log_fn("FOMOD installer detected — opening wizard...")
                saved_selections = None
                sel_path = None
                game_name = getattr(game, "name", "")
                if game_name:
                    sel_path = get_fomod_selections_path(game_name, mod_name)
                    if sel_path.is_file():
                        try:
                            with open(sel_path, "r", encoding="utf-8") as f:
                                saved_selections = json.load(f)
                            log_fn("Restored previous FOMOD selections.")
                        except (OSError, ValueError):
                            saved_selections = None

                if clear_progress_fn is not None:
                    clear_progress_fn()
                fomod_dialog_active.set()
                try:
                    if threading.current_thread() is threading.main_thread():
                        import tkinter as tk
                        container = getattr(parent_window, '_mod_panel_container', None) or parent_window
                        _done_var = tk.BooleanVar(value=False)
                        _result_holder: list = [None]

                        def _on_done(result):
                            _result_holder[0] = result
                            _done_var.set(True)

                        panel = FomodDialog(container, config, mod_root,
                                            installed_files=installed_files,
                                            active_files=active_files,
                                            saved_selections=saved_selections,
                                            selections_path=sel_path,
                                            on_done=_on_done)
                        try:
                            if panel.winfo_exists():
                                panel.place(relx=0, rely=0, relwidth=1, relheight=1)
                                panel.lift()
                                panel.focus_set()
                                parent_window.wait_variable(_done_var)
                        except Exception:
                            import traceback as _tb; _tb.print_exc()
                        dialog_result = _result_holder[0]
                    else:
                        with _interactive_dialog_lock:
                            result_holder = [None]
                            done_event = threading.Event()
                            parent_window.after(
                                0,
                                lambda: _show_fomod_dialog_on_main(
                                    parent_window, config, mod_root,
                                    installed_files, active_files,
                                    saved_selections, sel_path,
                                    result_holder, done_event,
                                ),
                            )
                            done_event.wait()
                            dialog_result = result_holder[0]
                finally:
                    fomod_dialog_active.clear()

                if dialog_result is None:
                    log_fn("FOMOD install cancelled.")
                    return

                if game_name:
                    sel_path = get_fomod_selections_path(game_name, mod_name)
                    try:
                        with open(sel_path, "w", encoding="utf-8") as f:
                            json.dump(dialog_result, f, indent=2)
                    except OSError:
                        pass
                _write_profile_fomod(dialog_result)

                final_selections = dialog_result

            file_list = resolve_files(config, final_selections, installed_files)
            is_fomod_install = True
            log_fn(f"FOMOD complete — {len(file_list)} file(s) to install.")

            if load_dev_mode():
                log_fn("[FOMOD DEV] === Selections ===")
                for step_key, group_map in final_selections.items():
                    # step_key may be a str(int) index or a step name
                    try:
                        step_idx = int(step_key)
                        step_label = config.steps[step_idx].name if step_idx < len(config.steps) else step_key
                    except (ValueError, IndexError):
                        step_label = step_key
                    for group_name, chosen in group_map.items():
                        log_fn(f"[FOMOD DEV]   Step '{step_label}' / Group '{group_name}': {chosen}")
                log_fn("[FOMOD DEV] === Files to install ===")
                for src, dst, is_folder in file_list:
                    kind = "[dir]" if is_folder else "[file]"
                    log_fn(f"[FOMOD DEV]   {kind} {src!r} → {dst!r}")
        elif getattr(game, "mod_supports_bundles", False) and detect_bundle(_unwrap_single_folder(extract_dir)):
            # --- Bundle install ---
            extract_dir = _unwrap_single_folder(extract_dir)
            bundle_name, variants = detect_bundle(extract_dir)
            log_fn(f"Bundle detected: '{bundle_name}' with {len(variants)} variant(s).")

            # Resolve profile dir once
            if mod_panel is not None and mod_panel._modlist_path is not None:
                _profile_dir = mod_panel._modlist_path.parent
            elif profile_dir is not None:
                _profile_dir = profile_dir
            else:
                _profile_dir = game.get_profile_root() / "profiles" / "default"
            modlist_path = _profile_dir / "modlist.txt"

            strip_prefixes     = getattr(game, "mod_folder_strip_prefixes", set())
            post_strip         = getattr(game, "mod_folder_strip_prefixes_post", set())
            required           = getattr(game, "mod_required_top_level_folders", set())
            auto_strip         = getattr(game, "mod_auto_strip_until_required", False)
            install_as_is      = getattr(game, "mod_install_as_is_if_no_match", False)
            install_prefix     = getattr(game, "mod_install_prefix", "")
            required_lower     = {r.lower() for r in required}
            required_file_types = getattr(game, "mod_required_file_types", set())

            installed_variant_names: list[str] = []
            for v_idx, (v_name, v_path) in enumerate(variants):
                v_mod_name = f"{bundle_name}__{v_name}"
                v_file_list = _resolve_direct_files(v_path)

                # Apply the same strip / prefix / required-folder logic as normal install
                if strip_prefixes:
                    v_file_list = _apply_strip_prefixes_to_file_list(v_file_list, strip_prefixes)
                if install_prefix:
                    _pfx = install_prefix.strip().strip("/").replace("\\", "/")
                    _pfx_parts = _pfx.lower().split("/")
                    new_vfl = []
                    for s, d, f in v_file_list:
                        d_parts = d.replace("\\", "/").split("/")
                        d_parts_lower = [p.lower() for p in d_parts]
                        if d_parts_lower[0] in required_lower:
                            new_vfl.append((s, d, f))
                            continue
                        match_len = 0
                        for i in range(len(_pfx_parts), 0, -1):
                            if d_parts_lower[:i] == _pfx_parts[-i:]:
                                match_len = i
                                break
                        missing = "/".join(_pfx.split("/")[:len(_pfx_parts) - match_len])
                        new_vfl.append((s, f"{missing}/{d}" if missing else d, f))
                    v_file_list = new_vfl
                if required and not _check_mod_top_level(v_file_list, required):
                    if auto_strip:
                        v_file_list, _ = _try_auto_strip_top_level(v_file_list, required)
                    if not v_file_list and install_as_is:
                        v_file_list = _resolve_direct_files(v_path)
                if post_strip:
                    v_file_list = _apply_strip_prefixes_to_file_list(v_file_list, post_strip)

                v_dest = game.get_effective_mod_staging_path() / v_mod_name
                if v_dest.exists():
                    import shutil as _shutil
                    def _frc(func, path, _exc):
                        os.chmod(path, 0o700)
                        func(path)
                    _shutil.rmtree(v_dest, onexc=_frc)
                _copy_file_list(v_file_list, v_path, v_dest, log_fn)
                _stamp_meta_install_date(v_dest / "meta.ini",
                                         installation_file=os.path.basename(archive_path))

                # Update mod index for this variant
                if not skip_index_update:
                    try:
                        _strip_fs = frozenset(s.lower() for s in (strip_prefixes or []))
                        _exts_fs  = frozenset(e.lower() for e in (getattr(game, "install_extensions", None) or []))
                        _root_fs  = frozenset(s.lower() for s in (getattr(game, "root_deploy_folders", None) or []))
                        _, _nf, _rf = _scan_dir(v_mod_name, str(v_dest), _strip_fs, _exts_fs, _root_fs)
                        _index_path = _profile_dir / "modindex.bin"
                        _norm_case = getattr(game, "normalize_folder_case", True)
                        update_mod_index(_index_path, v_mod_name, _nf, _rf, normalize_folder_case=_norm_case)
                    except Exception:
                        pass

                log_fn(f"  Variant '{v_name}' → {v_dest}")
                installed_variant_names.append(v_mod_name)

            # Insert a locked separator + all locked variants as a block at the
            # top of modlist.txt.  All variants are enabled by default — users
            # can disable whichever ones they don't want.
            sep_name = f"{bundle_name}_separator"
            existing = read_modlist(modlist_path)
            # Remove any pre-existing entries for this bundle's separator/variants
            remove_names = {sep_name} | set(installed_variant_names)
            existing = [e for e in existing if e.name not in remove_names]
            # Build the bundle block: separator + variants (not locked — users
            # can still toggle them; drag prevention is handled by the panel).
            bundle_block: list[ModEntry] = [
                ModEntry(name=sep_name, enabled=True, locked=True, is_separator=True),
            ]
            for vn in installed_variant_names:
                bundle_block.append(
                    ModEntry(name=vn, enabled=True, locked=False, is_separator=False)
                )
            # Prepend the bundle block at the top
            existing = bundle_block + existing
            write_modlist(modlist_path, existing)

            # Auto-lock the separator so it drags as a block
            locks = read_separator_locks(_profile_dir)
            locks[sep_name] = True
            write_separator_locks(_profile_dir, locks)

            log_fn(f"Installed bundle '{bundle_name}' ({len(variants)} variant(s)).")
            if not headless:
                _show_mod_notification(parent_window, f"Installed bundle: {bundle_name}")
            _fire_on_installed(on_installed, is_fomod=False)
            if mod_panel is not None and not headless:
                mod_panel.after(0, mod_panel.reload_after_install)
            # Bundles install one separator + N variants — renaming the
            # separator or individual variants doesn't fit the single-name
            # prompt cleanly, so skip the post-install rename here.
            return installed_variant_names[0] if installed_variant_names else mod_name
        elif getattr(game, "mod_supports_bundles", False) and detect_multi_mod(_unwrap_single_folder(extract_dir)):
            # --- Multi-mod archive: each subdir is a separate independent mod ---
            extract_dir = _unwrap_single_folder(extract_dir)
            multi_mods = detect_multi_mod(extract_dir)
            log_fn(f"Multi-mod archive detected: {len(multi_mods)} mod(s).")

            if mod_panel is not None and mod_panel._modlist_path is not None:
                _profile_dir = mod_panel._modlist_path.parent
            elif profile_dir is not None:
                _profile_dir = profile_dir
            else:
                _profile_dir = game.get_profile_root() / "profiles" / "default"
            modlist_path = _profile_dir / "modlist.txt"

            strip_prefixes     = getattr(game, "mod_folder_strip_prefixes", set())
            post_strip         = getattr(game, "mod_folder_strip_prefixes_post", set())
            required           = getattr(game, "mod_required_top_level_folders", set())
            auto_strip         = getattr(game, "mod_auto_strip_until_required", False)
            install_as_is      = getattr(game, "mod_install_as_is_if_no_match", False)
            install_prefix     = getattr(game, "mod_install_prefix", "")
            required_lower     = {r.lower() for r in required}

            installed_names: list[str] = []
            for m_name, m_path in multi_mods:
                m_file_list = _resolve_direct_files(m_path)
                if strip_prefixes:
                    m_file_list = _apply_strip_prefixes_to_file_list(m_file_list, strip_prefixes)
                if install_prefix:
                    _pfx = install_prefix.strip().strip("/").replace("\\", "/")
                    _pfx_parts = _pfx.lower().split("/")
                    new_mfl = []
                    for s, d, f in m_file_list:
                        d_parts = d.replace("\\", "/").split("/")
                        d_parts_lower = [p.lower() for p in d_parts]
                        if d_parts_lower[0] in required_lower:
                            new_mfl.append((s, d, f))
                            continue
                        match_len = 0
                        for i in range(len(_pfx_parts), 0, -1):
                            if d_parts_lower[:i] == _pfx_parts[-i:]:
                                match_len = i
                                break
                        missing = "/".join(_pfx.split("/")[:len(_pfx_parts) - match_len])
                        new_mfl.append((s, f"{missing}/{d}" if missing else d, f))
                    m_file_list = new_mfl
                if required and not _check_mod_top_level(m_file_list, required):
                    if auto_strip:
                        m_file_list, _ = _try_auto_strip_top_level(m_file_list, required)
                    if not m_file_list and install_as_is:
                        m_file_list = _resolve_direct_files(m_path)
                if post_strip:
                    m_file_list = _apply_strip_prefixes_to_file_list(m_file_list, post_strip)

                m_dest = game.get_effective_mod_staging_path() / m_name
                if m_dest.exists():
                    import shutil as _shutil
                    def _frc(func, path, _exc):
                        os.chmod(path, 0o700)
                        func(path)
                    _shutil.rmtree(m_dest, onexc=_frc)
                _copy_file_list(m_file_list, m_path, m_dest, log_fn)
                _stamp_meta_install_date(m_dest / "meta.ini",
                                         installation_file=os.path.basename(archive_path))

                if not skip_index_update:
                    try:
                        _strip_fs = frozenset(s.lower() for s in (strip_prefixes or []))
                        _exts_fs  = frozenset(e.lower() for e in (getattr(game, "install_extensions", None) or []))
                        _root_fs  = frozenset(s.lower() for s in (getattr(game, "root_deploy_folders", None) or []))
                        _, _nf, _rf = _scan_dir(m_name, str(m_dest), _strip_fs, _exts_fs, _root_fs)
                        _index_path = _profile_dir / "modindex.bin"
                        _norm_case = getattr(game, "normalize_folder_case", True)
                        update_mod_index(_index_path, m_name, _nf, _rf, normalize_folder_case=_norm_case)
                    except Exception:
                        pass

                prepend_mod(modlist_path, m_name, enabled=True)
                log_fn(f"  Installed '{m_name}' → {m_dest}")
                installed_names.append(m_name)

            log_fn(f"Installed {len(multi_mods)} mod(s) from archive.")
            if not headless:
                _show_mod_notification(parent_window, f"Installed {len(multi_mods)} mods")
            _fire_on_installed(on_installed, is_fomod=False)
            if mod_panel is not None and not headless:
                mod_panel.after(0, mod_panel.reload_after_install)
            return installed_names[0] if installed_names else mod_name
        else:
            mod_root = extract_dir
            file_list = _resolve_direct_files(extract_dir)
            log_fn(f"Direct install — {len(file_list)} file(s) to install.")

        dest_root = game.get_effective_mod_staging_path() / mod_name
        replace_selected_only = False
        replace_all = False
        if dest_root.exists():
            if headless and overwrite_existing:
                # Append-with-overwrite: delete the existing folder and reinstall cleanly.
                def _force_remove(func, path, _exc):
                    func(path)
                shutil.rmtree(dest_root, onexc=_force_remove)
                log_fn(f"Collection install: removed existing '{mod_name}' for overwrite reinstall.")
            elif headless and overwrite_existing is None:
                # In headless (collection new-profile) installs, a pre-existing folder
                # means the mod was already installed (e.g. two collection entries share
                # the same archive, or a previous partial run installed it).
                # Just return the folder name — no dialog, no re-extraction.
                log_fn(f"Collection install: '{mod_name}' folder already exists — skipping re-install.")
                return mod_name
            else:
                if threading.current_thread() is threading.main_thread():
                    replace_dialog = _ReplaceModDialog(parent_window, mod_name)
                    parent_window.wait_window(replace_dialog)
                else:
                    with _interactive_dialog_lock:
                        _rh: list = [None]
                        _ev = threading.Event()
                        parent_window.after(0, lambda: _show_replace_dialog_on_main(parent_window, mod_name, _rh, _ev))
                        _ev.wait()
                        replace_dialog = _rh[0]
                if replace_dialog is None or replace_dialog.result == "cancel":
                    log_fn(f"Install cancelled — '{mod_name}' already exists.")
                    return
                if replace_dialog.result == "rename":
                    mod_name = replace_dialog.new_name
                    dest_root = game.get_effective_mod_staging_path() / mod_name
                elif replace_dialog.result == "selected":
                    replace_selected_only = True
                elif replace_dialog.result == "all":
                    replace_all = True

        if replace_selected_only:
            expanded = _expand_folders_for_dialog(file_list, mod_root)
            if threading.current_thread() is threading.main_thread():
                sel_dialog = _SelectFilesDialog(parent_window, expanded)
                parent_window.wait_window(sel_dialog)
            else:
                with _interactive_dialog_lock:
                    _rh2: list = [None]
                    _ev2 = threading.Event()
                    parent_window.after(0, lambda: _show_select_files_dialog_on_main(parent_window, expanded, _rh2, _ev2))
                    _ev2.wait()
                    sel_dialog = _rh2[0]
            if sel_dialog is None or sel_dialog.result is None:
                log_fn("Install cancelled — no files selected.")
                return None
            chosen = sel_dialog.result
            file_list = [(s, d, f) for s, d, f in expanded if d in chosen]
            log_fn(f"Replace selected: {len(file_list)} file(s) chosen.")

        strip_prefixes = getattr(game, "mod_folder_strip_prefixes", set())
        if strip_prefixes:
            file_list = _apply_strip_prefixes_to_file_list(file_list, strip_prefixes)

        required = getattr(game, "mod_required_top_level_folders", set())
        required_lower = {r.lower() for r in required}

        install_prefix = getattr(game, "mod_install_prefix", "")
        if install_prefix:
            install_prefix = install_prefix.strip().strip("/").replace("\\", "/")
            prefix_parts = install_prefix.lower().split("/")
            new_file_list = []
            for s, d, f in file_list:
                d_parts = d.replace("\\", "/").split("/")
                d_parts_lower = [p.lower() for p in d_parts]
                # Skip prefix if the top-level folder is already a required folder
                if d_parts_lower[0] in required_lower:
                    new_file_list.append((s, d, f))
                    continue
                match_len = 0
                for i in range(len(prefix_parts), 0, -1):
                    if d_parts_lower[:i] == prefix_parts[-i:]:
                        match_len = i
                        break
                missing = "/".join(install_prefix.split("/")[:len(prefix_parts) - match_len])
                if missing:
                    new_file_list.append((s, f"{missing}/{d}", f))
                else:
                    new_file_list.append((s, d, f))
            file_list = new_file_list
            log_fn(f"Auto-prefixed mod files under '{install_prefix}/' (where needed).")
        required_file_types = getattr(game, "mod_required_file_types", set())
        auto_strip = getattr(game, "mod_auto_strip_until_required", False)
        install_as_is = getattr(game, "mod_install_as_is_if_no_match", False)
        did_auto_strip = False
        if not is_fomod_install and required and not _check_mod_top_level(file_list, required):
            if auto_strip:
                file_list, did_auto_strip = _try_auto_strip_top_level(file_list, required)
                if did_auto_strip:
                    log_fn("Auto-stripped top-level folder(s) so mod matches expected structure.")
            if not did_auto_strip and required_file_types:
                if _check_mod_top_level_file_types(file_list, required_file_types):
                    did_auto_strip = True
                    log_fn("Mod contains recognised top-level file type(s) — skipping prefix check.")
                elif auto_strip:
                    file_list, did_auto_strip = _try_auto_strip_for_file_types(file_list, required_file_types)
                    if did_auto_strip:
                        log_fn("Auto-stripped top-level folder(s) to expose recognised file type(s).")
            if not did_auto_strip:
                if install_as_is:
                    log_fn("Mod structure unrecognised — installing as-is (no prefix applied).")
                else:
                    dlg_result = None
                    if threading.current_thread() is threading.main_thread():
                        dlg = _SetPrefixDialog(parent_window, required, file_list, mod_name=mod_name)
                        parent_window.wait_window(dlg)
                        dlg_result = dlg.result
                    else:
                        with _interactive_dialog_lock:
                            result_holder = [None]
                            done_event = threading.Event()
                            parent_window.after(
                                0,
                                lambda: _show_set_prefix_dialog_on_main(
                                    parent_window, required, file_list, mod_name,
                                    result_holder, done_event,
                                ),
                            )
                            done_event.wait()
                            dlg_result = result_holder[0]
                    if dlg_result is None:
                        log_fn("Install cancelled — mod structure not mapped.")
                        return
                    action, prefix = dlg_result
                    if action == "prefix" and prefix:
                        prefix = prefix.strip().strip("/").replace("\\", "/")
                        file_list = [(s, f"{prefix}/{d}", f) for s, d, f in file_list]
                        log_fn(f"Remapped mod files under '{prefix}/'.")
        elif not is_fomod_install and not required and required_file_types and not _check_mod_top_level_file_types(file_list, required_file_types):
            if auto_strip:
                file_list, did_auto_strip = _try_auto_strip_for_file_types(file_list, required_file_types)
                if did_auto_strip:
                    log_fn("Auto-stripped top-level folder(s) to expose recognised file type(s).")
            if not did_auto_strip:
                if install_as_is:
                    log_fn("Mod structure unrecognised — installing as-is (no prefix applied).")
                else:
                    dlg_result = None
                    if threading.current_thread() is threading.main_thread():
                        dlg = _SetPrefixDialog(parent_window, set(), file_list, mod_name=mod_name)
                        parent_window.wait_window(dlg)
                        dlg_result = dlg.result
                    else:
                        with _interactive_dialog_lock:
                            result_holder = [None]
                            done_event = threading.Event()
                            parent_window.after(
                                0,
                                lambda: _show_set_prefix_dialog_on_main(
                                    parent_window, set(), file_list, mod_name,
                                    result_holder, done_event,
                                ),
                            )
                            done_event.wait()
                            dlg_result = result_holder[0]
                    if dlg_result is None:
                        log_fn("Install cancelled — mod structure not mapped.")
                        return
                    action, prefix = dlg_result
                    if action == "prefix" and prefix:
                        prefix = prefix.strip().strip("/").replace("\\", "/")
                        file_list = [(s, f"{prefix}/{d}", f) for s, d, f in file_list]
                        log_fn(f"Remapped mod files under '{prefix}/'.")

        post_strip_prefixes = getattr(game, "mod_folder_strip_prefixes_post", set())
        if post_strip_prefixes:
            file_list = _apply_strip_prefixes_to_file_list(file_list, post_strip_prefixes)

        dest_root = game.get_effective_mod_staging_path() / mod_name
        was_existing_mod = dest_root.exists()
        if replace_all and dest_root.exists():
            def _force_remove(func, path, _exc):
                os.chmod(path, 0o700)
                func(path)
            shutil.rmtree(dest_root, onexc=_force_remove)
            log_fn(f"Cleared existing mod folder for clean reinstall.")
        _copy_file_list(file_list, mod_root, dest_root, log_fn)
        log_fn(f"Installed '{mod_name}' → {dest_root}")

        if is_fomod_install and load_dev_mode():
            log_fn("[FOMOD DEV] === Post-install verification ===")
            missing: list[str] = []
            for src, dst, is_folder in file_list:
                dst_norm = dst.replace("\\", "/").rstrip("/")
                if not dst_norm:
                    continue
                expected = dest_root / Path(dst_norm)
                if is_folder:
                    if not expected.is_dir():
                        missing.append(f"[dir]  {dst_norm!r}")
                else:
                    if not expected.is_file():
                        # Destination may have been a trailing-slash folder install
                        src_name = Path(src.replace("\\", "/")).name
                        alt = dest_root / Path(dst_norm) / src_name
                        if not alt.is_file():
                            missing.append(f"[file] {dst_norm!r}")
            if missing:
                log_fn(f"[FOMOD DEV] WARNING — {len(missing)} expected file(s) not found after install:")
                for m in missing:
                    log_fn(f"[FOMOD DEV]   MISSING {m}")
            else:
                log_fn(f"[FOMOD DEV] All {len(file_list)} FOMOD file(s) verified present.")

        _stamp_meta_install_date(dest_root / "meta.ini",
                                  installation_file=os.path.basename(archive_path),
                                  is_fomod=is_fomod_install)

        for fn in getattr(game, "additional_install_logic", []):
            try:
                fn(dest_root, mod_name, log_fn)
            except Exception as e:
                log_fn(f"Additional install logic failed: {e}")

        # Resolve which profile directory to write modlist/plugins into.
        # Priority: explicit profile_dir arg > mod_panel's path > default.
        if mod_panel is not None and mod_panel._modlist_path is not None:
            _profile_dir = mod_panel._modlist_path.parent
        elif profile_dir is not None:
            _profile_dir = profile_dir
        else:
            _profile_dir = game.get_profile_root() / "profiles" / "default"
        modlist_path = _profile_dir / "modlist.txt"

        # Update the mod index for just this mod so the next filemap rebuild
        # reads from the index instead of rescanning all mod folders.
        # Skipped for collection installs (skip_index_update=True) — the
        # collection does one bulk rebuild_mod_index after all mods are done,
        # which is far faster than N concurrent read→merge→write passes.
        if not skip_index_update:
            try:
                _strip = frozenset(s.lower() for s in (getattr(game, "strip_prefixes", None) or []))
                _exts  = frozenset(e.lower() for e in (getattr(game, "install_extensions", None) or []))
                _root  = frozenset(s.lower() for s in (getattr(game, "root_deploy_folders", None) or []))
                _, normal_files, root_files = _scan_dir(
                    mod_name, str(dest_root), _strip, _exts, _root,
                )
                if mod_panel is not None and mod_panel._modlist_path is not None:
                    _ml = mod_panel._modlist_path
                else:
                    _ml = game.get_profile_root() / "profiles" / "default" / "modlist.txt"
                _index_path = _ml.parent / "modindex.bin"
                _norm_case = getattr(game, "normalize_folder_case", True)
                update_mod_index(_index_path, mod_name, normal_files, root_files,
                                 normalize_folder_case=_norm_case)
            except (OSError, ValueError, KeyError):
                pass  # non-fatal — next rebuild will fall back to a full rescan

        plugin_exts = getattr(game, "plugin_extensions", [])
        if plugin_exts and mod_panel is not None and mod_panel._modlist_path is not None:
            plugins_path = mod_panel._modlist_path.parent / "plugins.txt"
            loadorder_path = mod_panel._modlist_path.parent / "loadorder.txt"
            _sp = getattr(game, "plugins_use_star_prefix", True)
            exts_lower = {ext.lower() for ext in plugin_exts}
            new_plugins: list[str] = []
            if dest_root.is_dir():
                for entry in dest_root.iterdir():
                    if entry.is_file() and entry.suffix.lower() in exts_lower:
                        # Normalise extension to lowercase (e.g. plugin.ESP → plugin.esp)
                        if entry.suffix != entry.suffix.lower():
                            normalised = entry.with_suffix(entry.suffix.lower())
                            entry.rename(normalised)
                            entry = normalised
                        append_plugin(plugins_path, entry.name, enabled=True, star_prefix=_sp)
                        new_plugins.append(entry.name)
            if new_plugins:
                existing_lo = read_loadorder(loadorder_path)
                existing_lo_lower = {n.lower() for n in existing_lo}
                for name in new_plugins:
                    if name.lower() not in existing_lo_lower:
                        existing_lo.append(name)
                        existing_lo_lower.add(name.lower())
                write_loadorder(loadorder_path, [PluginEntry(name=n, enabled=True) for n in existing_lo])
                log_fn(f"plugins.txt / loadorder.txt: added {len(new_plugins)} plugin(s) from '{mod_name}'.")

            if was_existing_mod:
                ensure_mod_preserving_position(modlist_path, mod_name, enabled=True)
            else:
                prepend_mod(modlist_path, mod_name, enabled=True)

            log_fn(f"Added '{mod_name}' to modlist.")

        meta_path = dest_root / "meta.ini"
        _archive = Path(archive_path)
        _game_domain = getattr(game, "nexus_game_domain", "")
        if prebuilt_meta is not None:
            # Caller already has full metadata — write it directly, no API calls needed.
            try:
                prebuilt_meta.installation_file = os.path.basename(archive_path)
                write_meta(meta_path, prebuilt_meta)
                log_fn(f"Nexus: Saved metadata for '{mod_name}' "
                       f"(mod {prebuilt_meta.mod_id})")
            except OSError:
                pass
        elif _game_domain and _archive.is_file():
            def _detect_meta():
                try:
                    app = None
                    try:
                        app = parent_window.winfo_toplevel()
                    except Exception:
                        pass
                    api = getattr(app, "_nexus_api", None) if app else None

                    meta = resolve_nexus_meta_for_archive(
                        _archive, _game_domain,
                        api=api,
                        log_fn=lambda m: (
                            app.after(0, lambda msg=m: log_fn(msg))
                            if app else None
                        ),
                    )
                    if meta:
                        write_meta(meta_path, meta)
                        msg = f"Nexus: Saved metadata for '{mod_name}' (mod {meta.mod_id})"
                        if app:
                            app.after(0, lambda: log_fn(msg))
                except Exception:
                    pass
            threading.Thread(target=_detect_meta, daemon=True).start()

        if not headless:
            _show_mod_notification(parent_window, f"Installed: {mod_name}")

        _fire_on_installed(on_installed, is_fomod=is_fomod_install)

        if mod_panel is not None and not headless:
            mod_panel.after(0, mod_panel.reload_after_install)

        _maybe_queue_rename_after_install(
            parent_window, mod_panel, headless=headless,
            mod_name=mod_name, suggestions=suggestions,
        )

        return mod_name

    except Exception as e:
        import traceback
        log_fn(f"Install error: {e}")
        log_fn(traceback.format_exc())
        return None
    finally:
        shutil.rmtree(extract_dir, ignore_errors=True)
        if _tmp_claimed:
            with _tmp_space_lock:
                _tmp_space_reserved = max(0, _tmp_space_reserved - _extract_size_estimate)
