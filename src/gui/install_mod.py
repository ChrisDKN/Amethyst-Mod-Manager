"""
Install mod from archive: extract, FOMOD wizard, strip/prefix, copy to staging, update modlist/plugins.
Used by ModListPanel, PluginPanel, TopBar, and App. Imports dialogs and mod_name_utils.
"""

import json
import os
import shutil
import tarfile
import tempfile
import threading
import zipfile
from pathlib import Path
from datetime import datetime

import py7zr

from gui.dialogs import (
    _ReplaceModDialog,
    _SelectFilesDialog,
    _SetPrefixDialog,
)
from gui.fomod_dialog import FomodDialog
from gui.mod_name_utils import _strip_title_metadata, _suggest_mod_names
from Utils.fomod_parser import detect_fomod, parse_module_config
from Utils.fomod_installer import resolve_files
from Utils.config_paths import get_fomod_selections_path
from Utils.plugins import read_plugins, append_plugin
from Utils.modlist import prepend_mod, ensure_mod_preserving_position
from Utils.filemap import _scan_dir, update_mod_index
from Nexus.nexus_meta import write_meta, resolve_nexus_meta_for_archive


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
        d = dst_rel.replace("\\", "/").strip("/")
        while "/" in d:
            first, remainder = d.split("/", 1)
            if first.lower() in strip_lower:
                d = remainder
            else:
                break
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
    max_strip_depth: int = 5,
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


def _stamp_meta_install_date(meta_ini_path: Path) -> None:
    """Write the current datetime as the ``installed`` key in meta.ini if not already present."""
    import configparser as _cp
    parser = _cp.ConfigParser()
    if meta_ini_path.is_file():
        parser.read(str(meta_ini_path), encoding="utf-8")
    if not parser.has_section("General"):
        parser.add_section("General")
    if not parser.get("General", "installed", fallback=""):
        parser.set("General", "installed",
                   datetime.now().strftime("%Y-%m-%dT%H:%M:%S"))
        meta_ini_path.parent.mkdir(parents=True, exist_ok=True)
        with open(meta_ini_path, "w", encoding="utf-8") as fh:
            parser.write(fh)


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


def _copy_file_list(file_list: list[tuple[str, str, bool]],
                    src_root: str, dest_root: Path, log_fn) -> None:
    """
    Copy each (source, destination, is_folder) entry from src_root into dest_root.
    """
    copied = 0
    for src_rel, dst_rel, is_folder in file_list:
        src = Path(src_root) / src_rel
        dst = dest_root / dst_rel

        if is_folder:
            if src.is_dir():
                shutil.copytree(src, dst, dirs_exist_ok=True)
                copied += 1
        else:
            if src.is_file():
                dst.parent.mkdir(parents=True, exist_ok=True)
                if dst.exists():
                    dst.chmod(0o644)
                    dst.unlink()
                shutil.copy2(src, dst)
                copied += 1

    log_fn(f"Copied {copied} item(s) to staging area.")


def install_mod_from_archive(archive_path: str, parent_window, log_fn,
                             game, mod_panel=None) -> None:
    """
    Extract archive to a temp directory, detect FOMOD, run the wizard if
    present, then copy the resolved files into the game's mod staging area.
    Supports .zip, .7z, and .tar.* formats.
    """
    ext = archive_path.lower()
    raw_stem = os.path.splitext(os.path.basename(archive_path))[0]
    if raw_stem.endswith(".tar"):
        raw_stem = os.path.splitext(raw_stem)[0]

    suggestions = _suggest_mod_names(raw_stem)
    mod_name = suggestions[0] if suggestions else raw_stem

    extract_dir = tempfile.mkdtemp(prefix="modmgr_")

    try:
        if ext.endswith(".zip"):
            with zipfile.ZipFile(archive_path, "r") as z:
                z.extractall(extract_dir)
        elif ext.endswith(".7z"):
            try:
                with py7zr.SevenZipFile(archive_path, "r") as z:
                    z.extractall(extract_dir)
            except Exception as e7:
                log_fn(f"py7zr failed ({e7}), retrying with libarchive…")
                shutil.rmtree(extract_dir, ignore_errors=True)
                os.makedirs(extract_dir, exist_ok=True)
                import libarchive
                prev_cwd = os.getcwd()
                try:
                    os.chdir(extract_dir)
                    libarchive.extract_file(archive_path)
                finally:
                    os.chdir(prev_cwd)
        elif any(ext.endswith(s) for s in (".tar.gz", ".tar.bz2", ".tar.xz", ".tar")):
            with tarfile.open(archive_path, "r:*") as t:
                t.extractall(extract_dir)
        elif ext.endswith(".rar"):
            try:
                import rarfile
                with rarfile.RarFile(archive_path, "r") as r:
                    r.extractall(extract_dir)
            except (ImportError, Exception) as e_rar:
                log_fn(f"rarfile failed ({e_rar}), trying libarchive…")
                shutil.rmtree(extract_dir, ignore_errors=True)
                os.makedirs(extract_dir, exist_ok=True)
                import libarchive
                with libarchive.file_reader(archive_path) as _arc:
                    for _entry in _arc:
                        _dest = os.path.join(extract_dir,
                                             _entry.pathname.lstrip("/").replace("/", os.sep))
                        if _entry.isdir:
                            os.makedirs(_dest, exist_ok=True)
                            continue
                        os.makedirs(os.path.dirname(_dest) or extract_dir, exist_ok=True)
                        with open(_dest, "wb") as _fh:
                            for _block in _entry.get_blocks():
                                _fh.write(_block)
        else:
            log_fn(f"Unsupported archive format: {os.path.basename(archive_path)}")
            log_fn("Supported formats: .zip, .7z, .rar, .tar.gz")
            return

        fomod_result = detect_fomod(extract_dir)
        if fomod_result:
            mod_root, config_path = fomod_result
            log_fn("FOMOD installer detected — opening wizard...")
            config = parse_module_config(config_path)

            if config.name:
                fomod_clean = _strip_title_metadata(config.name)
                seen = set()
                new_suggestions = []
                for c in (fomod_clean, config.name, *suggestions):
                    if c and c not in seen:
                        seen.add(c)
                        new_suggestions.append(c)
                suggestions = new_suggestions
                mod_name = suggestions[0]

            installed_files: set[str] = set()
            if mod_panel is not None and mod_panel._modlist_path is not None:
                plugins_path = mod_panel._modlist_path.parent / "plugins.txt"
                if plugins_path.is_file():
                    for entry in read_plugins(plugins_path):
                        if entry.enabled:
                            installed_files.add(entry.name.lower())

            saved_selections = None
            game_name = getattr(game, "name", "")
            if game_name:
                sel_path = get_fomod_selections_path(game_name, mod_name)
                if sel_path.is_file():
                    try:
                        with open(sel_path, "r", encoding="utf-8") as f:
                            saved_selections = json.load(f)
                        log_fn("Restored previous FOMOD selections.")
                    except Exception:
                        saved_selections = None

            dialog = FomodDialog(parent_window, config, mod_root,
                                 installed_files=installed_files,
                                 saved_selections=saved_selections)
            parent_window.wait_window(dialog)
            if dialog.result is None:
                log_fn("FOMOD install cancelled.")
                return

            if game_name:
                sel_path = get_fomod_selections_path(game_name, mod_name)
                try:
                    with open(sel_path, "w", encoding="utf-8") as f:
                        json.dump(dialog.result, f, indent=2)
                except Exception:
                    pass

            file_list = resolve_files(config, dialog.result, installed_files)
            log_fn(f"FOMOD complete — {len(file_list)} file(s) to install.")
        else:
            mod_root = extract_dir
            file_list = _resolve_direct_files(extract_dir)
            log_fn(f"Direct install — {len(file_list)} file(s) to install.")

        dest_root = game.get_mod_staging_path() / mod_name
        replace_selected_only = False
        replace_all = False
        if dest_root.exists():
            replace_dialog = _ReplaceModDialog(parent_window, mod_name)
            parent_window.wait_window(replace_dialog)
            if replace_dialog.result == "cancel":
                log_fn(f"Install cancelled — '{mod_name}' already exists.")
                return
            if replace_dialog.result == "selected":
                replace_selected_only = True
            elif replace_dialog.result == "all":
                replace_all = True

        if replace_selected_only:
            sel_dialog = _SelectFilesDialog(parent_window, file_list)
            parent_window.wait_window(sel_dialog)
            if sel_dialog.result is None:
                log_fn("Install cancelled — no files selected.")
                return
            chosen = sel_dialog.result
            file_list = [(s, d, f) for s, d, f in file_list if d in chosen]
            log_fn(f"Replace selected: {len(file_list)} file(s) chosen.")

        strip_prefixes = getattr(game, "mod_folder_strip_prefixes", set())
        if strip_prefixes:
            file_list = _apply_strip_prefixes_to_file_list(file_list, strip_prefixes)

        install_prefix = getattr(game, "mod_install_prefix", "")
        if install_prefix:
            install_prefix = install_prefix.strip().strip("/").replace("\\", "/")
            prefix_parts = install_prefix.lower().split("/")
            new_file_list = []
            for s, d, f in file_list:
                d_parts = d.replace("\\", "/").split("/")
                d_parts_lower = [p.lower() for p in d_parts]
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

        required = getattr(game, "mod_required_top_level_folders", set())
        did_auto_strip = False
        if required and not _check_mod_top_level(file_list, required):
            if getattr(game, "mod_auto_strip_until_required", False):
                file_list, did_auto_strip = _try_auto_strip_top_level(file_list, required)
                if did_auto_strip:
                    log_fn("Auto-stripped top-level folder(s) so mod matches expected structure.")
            if not did_auto_strip:
                dlg = _SetPrefixDialog(parent_window, required, file_list)
                parent_window.wait_window(dlg)
                if dlg.result is None:
                    log_fn("Install cancelled — mod structure not mapped.")
                    return
                action, prefix = dlg.result
                if action == "prefix" and prefix:
                    prefix = prefix.strip().strip("/").replace("\\", "/")
                    file_list = [(s, f"{prefix}/{d}", f) for s, d, f in file_list]
                    log_fn(f"Remapped mod files under '{prefix}/'.")

        dest_root = game.get_mod_staging_path() / mod_name
        was_existing_mod = dest_root.exists()
        if replace_all and dest_root.exists():
            shutil.rmtree(dest_root)
            log_fn(f"Cleared existing mod folder for clean reinstall.")
        _copy_file_list(file_list, mod_root, dest_root, log_fn)
        log_fn(f"Installed '{mod_name}' → {dest_root}")

        _stamp_meta_install_date(dest_root / "meta.ini")

        # Update the mod index for just this mod so the next filemap rebuild
        # reads from the index instead of rescanning all mod folders.
        try:
            _strip = frozenset(s.lower() for s in (getattr(game, "strip_prefixes", None) or []))
            _exts  = frozenset(e.lower() for e in (getattr(game, "install_extensions", None) or []))
            _root  = frozenset(s.lower() for s in (getattr(game, "root_deploy_folders", None) or []))
            _, normal_files, root_files = _scan_dir(
                mod_name, str(dest_root), _strip, _exts, _root,
            )
            _index_path = modlist_path.parent.parent.parent / "modindex.txt"
            update_mod_index(_index_path, mod_name, normal_files, root_files)
        except Exception:
            pass  # non-fatal — next rebuild will fall back to a full rescan

        plugin_exts = getattr(game, "plugin_extensions", [])
        if plugin_exts and mod_panel is not None and mod_panel._modlist_path is not None:
            plugins_path = mod_panel._modlist_path.parent / "plugins.txt"
            exts_lower = {ext.lower() for ext in plugin_exts}
            added = 0
            if dest_root.is_dir():
                for entry in dest_root.iterdir():
                    if entry.is_file() and entry.suffix.lower() in exts_lower:
                        append_plugin(plugins_path, entry.name, enabled=True)
                        added += 1
            if added:
                log_fn(f"plugins.txt: added {added} plugin(s) from '{mod_name}'.")

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

        meta_path = dest_root / "meta.ini"
        _archive = Path(archive_path)
        _game_domain = getattr(game, "nexus_game_domain", "")
        if _game_domain and _archive.is_file():
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

        if mod_panel is not None:
            mod_panel.reload_after_install()

    except Exception as e:
        import traceback
        log_fn(f"Install error: {e}")
        log_fn(traceback.format_exc())
    finally:
        shutil.rmtree(extract_dir, ignore_errors=True)
