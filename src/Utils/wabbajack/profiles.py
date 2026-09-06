from __future__ import annotations

import configparser
import copy
import json
import os
import shutil
from functools import lru_cache
from pathlib import Path

from .hashes import file_hash
from .manifest import qvalue, stock_folder
from .paths import WabbajackError, safe_name, source_path, within
from .adapters import adapter_for

_METADATA_INIS = {"settings.ini", "initweaks.ini", "savepath.ini", "custom.ini", "modorganizer.ini"}
_EXTENDERS = ("skse64_loader.exe", "sksevr_loader.exe", "f4se_loader.exe", "f4sevr_loader.exe",
              "nvse_loader.exe", "fose_loader.exe", "obse_loader.exe", "obse64_loader.exe")


def profile_names(request, store):
    names = dict(store.get("profile_names", {}))
    profiles = request.profiles or [request.package.name]
    parent = store.profile_root / "profiles"
    for profile in referenced_profiles(store.directory, store.profile_root):
        try:
            raw = json.loads((profile / "profile_state.json").read_text())
        except (OSError, ValueError):
            continue
        authored = raw.get("profile_settings", {}).get("wabbajack_profile")
        if authored in names and not (parent / names[authored]).exists():
            old = names[authored]
            names[authored] = profile.name
            for table in ("outputs", "journal", "baselines"):
                with store.db:
                    store.db.execute(f"UPDATE {table} SET path=? || substr(path, ?) WHERE substr(path, 1, ?)=?",
                        (f"profiles/{profile.name}/", len(f"profiles/{old}/") + 1,
                         len(f"profiles/{old}/"), f"profiles/{old}/"))
    used = {p.name.casefold() for p in parent.iterdir()} if parent.is_dir() else set()
    for authored in profiles:
        if authored in names:
            continue
        base = safe_name(request.package.name + (" - " + authored if len(profiles) > 1 else ""))
        name, number = base, 2
        while name.casefold() in used or name.casefold() == "default":
            name, number = f"{base} ({number})", number + 1
        names[authored] = name
        used.add(name.casefold())
    store.set("profile_names", names)
    return names


def prepare_profiles(request, store, reconstruction, desired):
    def copy_file(source, target):
        store._copy(source, target, stop=reconstruction.control.stop)
    names = profile_names(request, store)
    store.set("preserved_profiles", [name for authored, name in names.items() if authored not in request.profiles and request.package.profiles])
    output = reconstruction.output
    adapter = adapter_for(request.package, request.game)
    selected = request.profiles or [request.package.name]
    stock_rel = stock_folder(request.package)
    stock = source_path(output, stock_rel) if stock_rel else None
    translated = {}
    for authored in selected:
        name = names[authored]
        stage = store.work / "profiles" / name
        if stage.exists():
            shutil.rmtree(stage)
        stage.mkdir(parents=True, exist_ok=True)
        settings = {"profile_specific_mods": True, "wabbajack_install_id": store.get("id"),
                    "wabbajack_directory": str(store.directory), "wabbajack_profile": authored,
                    "wabbajack_adjustments": request.fixes}
        if stock:
            settings["game_path"] = str(store.root / stock.relative_to(output))
        state = {"profile_settings": settings}
        roots, strips, hidden = {}, {}, {}
        for key in desired:
            parts = key.split("/")
            if len(parts) >= 4 and parts[1].casefold() == "mods":
                mod, rel = parts[2], "/".join(parts[3:])
                if parts[3].casefold() == "root" and len(parts) > 4:
                    roots.setdefault(mod, []).append(rel.lower())
                    strips[mod] = [parts[3]]
                if rel.lower().endswith(".mohidden"):
                    hidden.setdefault(mod, []).append(rel.lower())
        if roots:
            state["root_mod_files"] = roots
            state["mod_strip_prefixes"] = strips
        if hidden:
            state["excluded_mod_files"] = hidden
        try:
            source_profile = source_path(output, f"profiles/{authored}") if request.package.profiles else None
        except (OSError, WabbajackError):
            source_profile = None
        inis = False
        if source_profile:
            cp = configparser.ConfigParser(interpolation=None, strict=False)
            try:
                config = source_path(source_profile, "settings.ini")
                cp.read(config, encoding="utf-8-sig")
                if cp.getboolean("General", "LocalSaves", fallback=False):
                    settings["profile_saves"] = True
            except (OSError, ValueError, configparser.Error):
                pass
            for path in source_profile.rglob("*"):
                if not path.is_file():
                    continue
                rel = path.relative_to(source_profile)
                if len(rel.parts) == 1 and path.suffix.lower() == ".ini":
                    if path.name.casefold() in _METADATA_INIS:
                        continue
                    rel = Path("ini files") / path.name
                    inis = True
                elif rel.parts[0].casefold() not in {"saves"} and path.name.casefold() not in {"modlist.txt", "plugins.txt", "loadorder.txt", "archives.txt", "lockedorder.txt"}:
                    continue
                target = within(stage, rel.as_posix())
                copy_file(path, target)
                if rel.parts[0].casefold() == "saves":
                    settings["profile_saves"] = True
        else:
            (stage / "modlist.txt").write_text("+Wabbajack Game Files\n", encoding="utf-8")
        if inis:
            settings["profile_ini_files"] = True
        for key, row in desired.items():
            rel = key.removeprefix("root/")
            dest = adapter.root_destination(rel)
            if dest:
                copy_file(Path(row["source"]), within(stage / "Root_Folder", dest))
            if rel.casefold().startswith("overwrite/"):
                copy_file(Path(row["source"]), within(stage / "overwrite", rel.split("/", 1)[1]))
        if not request.package.profiles:
            (stage / "modlist.txt").write_text("# Managed Wabbajack game layout\n", encoding="utf-8")
        extras, arguments, working_dirs = _executables(request, store, output, stock)
        state["custom_exes"] = extras
        state["wabbajack_working_directories"] = working_dirs
        if extras:
            selected_exe = next((Path(p).name for p in extras if Path(p).name.lower() in _EXTENDERS), None)
            if selected_exe:
                state["selected_exe"] = selected_exe
        (stage / "profile_state.json").write_text(json.dumps(state, indent=2), encoding="utf-8")
        if arguments:
            (stage / "exe_args.json").write_text(json.dumps(arguments, indent=2), encoding="utf-8")
        for path in stage.rglob("*"):
            if path.is_file():
                key = f"profiles/{name}/{path.relative_to(stage).as_posix()}"
                digest = file_hash(path)
                translated[key] = {"source": str(path), "authored_hash": digest,
                                   "signature": "profile:" + digest}
    desired.update(translated)
    return [store.profile_root / "profiles" / names[p] for p in selected]


def _executables(request, store, output, stock):
    extras, arguments, working_dirs = [], {}, {}
    game_root = store.root / stock.relative_to(output) if stock else Path(request.game_roots.get(request.package.game, request.game.get_game_path()))
    allowed = [store.root, *request.game_roots.values()]
    adapter = adapter_for(request.package, request.game)
    def resolve(value):
        from .runtime import host_path
        path = host_path(request.game, qvalue(value))
        if path is None:
            return None
        if not path.is_absolute():
            path = store.root / path
        if not any(path.resolve().is_relative_to(Path(root).resolve()) for root in allowed):
            return None
        if path.is_relative_to(store.root):
            parts = path.relative_to(store.root).parts
            deployed = adapter.root_destination(path.relative_to(store.root).as_posix())
            if deployed:
                path = game_root / deployed
            elif len(parts) > 3 and parts[0].lower() == "mods" and parts[2].lower() == "root":
                path = game_root.joinpath(*parts[3:])
        return path
    ini = output / "ModOrganizer.ini"
    if ini.is_file():
        cp = configparser.ConfigParser(interpolation=None, strict=False)
        cp.optionxform = str
        try:
            cp.read(ini, encoding="utf-8-sig")
            section = dict(cp["customExecutables"]) if cp.has_section("customExecutables") else {}
            for key, value in section.items():
                if not key.endswith("\\binary"):
                    continue
                p = resolve(value)
                if p is None or p.name.lower() in {"modorganizer.exe", "nxmhandler.exe"}:
                    continue
                extras.append(str(p))
                cwd = section.get(key.removesuffix("binary") + "workingDirectory", "")
                if cwd:
                    wd = resolve(cwd)
                    if wd:
                        working_dirs[p.name] = str(wd)
                args = qvalue(section.get(key.removesuffix("binary") + "arguments", ""))
                if args:
                    arguments[p.name] = args
        except (OSError, configparser.Error):
            pass
    for path in output.rglob("*.exe"):
        if path.name.lower() in _EXTENDERS:
            p = resolve(str(store.root / path.relative_to(output)))
            if p:
                extras.append(str(p))
                working_dirs[p.name] = str(game_root)
    return list(dict.fromkeys(extras)), arguments, working_dirs


def validate_links(store, profiles):
    mods = store.root / "mods"
    for profile in profiles:
        if profile.is_symlink():
            raise WabbajackError(f"Managed profile cannot be a symbolic link: {profile.name}")
        link = profile / "mods"
        if link.is_symlink() and link.resolve() != mods.resolve():
            raise WabbajackError(f"Profile mod storage changed: {profile.name}")
        if link.exists() and not link.is_symlink():
            raise WabbajackError(f"Profile mod storage is occupied: {profile.name}")


def publish_links(store, profiles):
    validate_links(store, profiles)
    mods = store.root / "mods"
    mods.mkdir(exist_ok=True)
    for profile in profiles:
        profile.mkdir(parents=True, exist_ok=True)
        link = profile / "mods"
        if link.is_symlink():
            if link.resolve() != mods.resolve():
                raise WabbajackError(f"Profile mod storage changed: {profile.name}")
        elif link.exists():
            raise WabbajackError(f"Profile mod storage is occupied: {profile.name}")
        else:
            link.symlink_to(os.path.relpath(mods, profile), target_is_directory=True)


def refresh_profiles(request, profiles, log):
    from Utils.filegraph.service import FileGraphService
    for profile in profiles:
        game = copy.copy(request.game)
        game.set_active_profile_dir(profile)
        game.load_paths()
        from Utils.profiles.state import read_profile_settings
        if read_profile_settings(profile).get("is_group"):
            from Utils.profiles.groups import materialize_group
            materialize_group(game, profile, log_fn=log)
        FileGraphService.open_library(game, profile, log_fn=log).refresh(profile)
    for profile in profiles:
        game = copy.copy(request.game)
        game.set_active_profile_dir(profile)
        game.load_paths()
        FileGraphService.open_library(game, profile, log_fn=log).ensure_ready(profile)


def referenced_profiles(directory, profile_root):
    profiles = profile_root / "profiles"
    result = []
    if profiles.is_dir():
        for profile in profiles.iterdir():
            if not profile.is_dir():
                continue
            try:
                raw = json.loads((profile / "profile_state.json").read_text())
                saved = raw.get("profile_settings", {}).get("wabbajack_directory", "")
                if saved and Path(saved).resolve() == directory.resolve():
                    result.append(profile)
                elif (profile / "mods").is_symlink() and (profile / "mods").resolve().is_relative_to(directory.resolve()):
                    result.append(profile)
            except (OSError, ValueError):
                if (profile / "mods").is_symlink() and (profile / "mods").resolve().is_relative_to(directory.resolve()):
                    result.append(profile)
        members = {p.name for p in result}
        for profile in profiles.iterdir():
            if profile in result or not profile.is_dir():
                continue
            try:
                settings = json.loads((profile / "profile_state.json").read_text()).get("profile_settings", {})
                if settings.get("is_group") and members.intersection(settings.get("group_members", [])):
                    result.append(profile)
            except (OSError, ValueError):
                pass
    return result


@lru_cache(maxsize=64)
def _group_members(profile, stamp):
    from Utils.profiles.state import read_profile_settings
    settings = read_profile_settings(profile)
    return settings.get("group_members", []) if settings.get("is_group") else []


def invalidate_shared_catalogs(library):
    candidates = [library.root]
    state_path = library.root / "profile_state.json"
    if state_path.is_file():
        stat = state_path.stat()
        for name in _group_members(library.root, (stat.st_mtime_ns, stat.st_ctime_ns, stat.st_size)):
            candidates.append(within(library.root.parent, name))
    directories = set()
    for profile in candidates:
        mods = profile / "mods"
        if mods.is_symlink():
            directory = mods.resolve().parent.parent
            if (directory / "state.sqlite").is_file() and directory.parent.name == ".wabbajack":
                directories.add(directory)
    if not directories:
        return
    from Utils.filegraph.service import _library_sessions, _library_guard, require_native
    with _library_guard:
        loaded = dict(_library_sessions)
    profiles = {p for directory in directories for p in referenced_profiles(directory, directory.parent.parent)}
    for profile in profiles:
        if profile.resolve() == library.root.resolve():
            continue
        other = loaded.get(str(profile.resolve()))
        native = other._native if other else require_native().LibrarySession.open(profile)
        native.set_ready(False)
        if other:
            other._variant_keys_cache = None
            for session in other._profiles.values():
                session._invalidate_resolution_cache()


def cleanup_unreferenced(profile_root):
    from .store import installations, Store
    for info in installations(profile_root):
        directory = Path(info["directory"])
        if info.get("status") != "complete" or referenced_profiles(directory, profile_root):
            continue
        store = Store(directory, profile_root)
        try:
            with store.exclusive():
                if not referenced_profiles(directory, profile_root):
                    store.close()
                    shutil.rmtree(directory)
                    store = None
        except WabbajackError:
            pass
        finally:
            if store is not None:
                store.close()
